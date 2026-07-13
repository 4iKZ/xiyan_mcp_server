"""
Stage 2 Schema 筛选服务

用本地小模型（如 qwen3-14B）从向量检索召回的 N 张候选表中精筛出 M 张最相关的表。

设计要点：
- 调用方式：OpenAI 兼容格式（通过 openai SDK 访问本地 vLLM/OpenAI-compat 端点）
- 失败行为：解析或调用失败时抛出 Stage2FilterError，不降级
              （便于实验中精确归因，调用方可决定是否吞掉异常）
- 输入信息粒度：仅表名 + friendly_name + description（轻量级，控制 prompt 长度）
"""
import asyncio
import json
import logging
import re
from typing import List, Dict, Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


# Stage2 精筛 LLM 调用的并发限流（独立于主 LLM 的 Semaphore）
# 原因：stage2 用的是 qwen3-32b（:8001），主 LLM 用的是 xiyan-32b（:18000），
# 两个 vLLM 端点的 max_num_seqs 互不影响；分别限流可让两侧都达到 2 并发。
_STAGE2_SEM = asyncio.Semaphore(2)


class Stage2FilterError(Exception):
    """Stage 2 筛选失败异常"""
    pass


class Stage2Filter:
    """
    Stage 2 Schema 精筛器

    用法：
        f = Stage2Filter(config)
        kept = await f.filter(query, candidates, top_m=5)
        # kept 是 candidates 的子集（保持原顺序），长度 ≤ top_m
    """

    # 精筛 prompt 模板
    PROMPT_TEMPLATE = """你是数据库 Schema 筛选专家。下面是 {n} 张候选数据表的信息，请从中选出最有可能用于回答用户问题的 {m} 张表。

# 用户问题
{query}

# 候选表（共 {n} 张）
{candidates_block}

# 输出要求
1. **必须返回恰好 {m} 个表名**，按相关性从高到低排列。即使你认为只有少数几张表强相关，也要把剩下相对最相关的表补齐到 {m} 个，**不允许少于 {m} 个**。这是为了下游配比实验的控制变量需要，不是让你判断"够不够用"。
2. 表名必须从上面候选表中精确选择，不要修改、不要添加前缀
3. 不要解释、不要 markdown 包裹、不要 ```json``` 标记
4. 直接输出形如 ["table_name_a", "table_name_b", ...] 的 JSON 数组，**长度严格等于 {m}**

# 你的输出
"""

    def __init__(self, config: dict):
        """
        初始化 Stage 2 筛选器

        Args:
            config: 配置字典，包含：
                - model_name: 模型名称
                - api_url: OpenAI 兼容 API 地址
                - api_key: API key（本地服务可填 "EMPTY"）
                - temperature: 采样温度（建议 0）
                - timeout: 请求超时（秒）
                - enable_thinking: 是否启用 Qwen3 thinking 模式（默认 False）
                  筛选任务不需要 thinking，开启反而会污染 JSON 解析并增加延迟
        """
        self.model_name = config.get("model_name", "qwen3-14b")
        self.api_url = config.get("api_url", "http://localhost:8002/v1/")
        self.api_key = config.get("api_key", "EMPTY") or "EMPTY"
        self.temperature = float(config.get("temperature", 0.0))
        self.timeout = float(config.get("timeout", 60))
        self.enable_thinking = bool(config.get("enable_thinking", False))

        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.api_url,
            timeout=self.timeout,
        )
        logger.info(
            f"Stage2Filter 初始化: model={self.model_name}, url={self.api_url}, "
            f"enable_thinking={self.enable_thinking}"
        )

    def _build_candidates_block(self, candidates: List[Dict]) -> str:
        """
        构建候选表信息块

        Args:
            candidates: 候选表列表，每个元素是 dict，至少含 table_name；
                        可选 embedding_content（优先）、friendly_name、description

        Returns:
            格式化的候选表信息字符串
        """
        lines = []
        for i, c in enumerate(candidates, 1):
            table_name = c.get("table_name", "")
            ec = c.get("embedding_content", "")
            line = f"{i}. {table_name}"
            if ec:
                ec_clean = ec.replace("\n", " ").strip()
                line += f"\n   {ec_clean}"
            else:
                friendly = c.get("friendly_name", "")
                desc = c.get("description", "")
                if friendly:
                    line += f"\n   友好名: {friendly}"
                if desc:
                    desc_short = desc.replace("\n", " ").strip()
                    if len(desc_short) > 200:
                        desc_short = desc_short[:200] + "..."
                    line += f"\n   描述: {desc_short}"
            lines.append(line)
        return "\n".join(lines)

    def _parse_table_names(self, raw: str) -> List[str]:
        """
        从模型输出中解析表名 JSON 数组

        容错策略：
        - 去除 markdown 代码块包裹
        - 用正则提取第一段形如 [...] 的 JSON 数组

        Args:
            raw: 模型原始输出

        Returns:
            表名列表

        Raises:
            Stage2FilterError: 解析失败
        """
        if not raw:
            raise Stage2FilterError("模型输出为空")

        text = raw.strip()
        # 防御：Qwen3 在 enable_thinking 未生效时仍会输出 <think>...</think> 段，
        # 先剥掉，避免污染后续 JSON 解析
        text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
        text = text.strip()
        # 去除 markdown 代码块
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        # 尝试直接解析
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed]
        except json.JSONDecodeError:
            pass

        # 正则兜底：提取第一段 [...]
        m = re.search(r"\[[^\[\]]*\]", text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed]
            except json.JSONDecodeError as e:
                raise Stage2FilterError(
                    f"JSON 解析失败: {e}; 原始输出={raw[:300]!r}"
                )

        raise Stage2FilterError(
            f"未能从模型输出中提取表名数组; 原始输出={raw[:300]!r}"
        )

    async def filter(
        self,
        query: str,
        candidates: List[Dict],
        top_m: int,
    ) -> List[Dict]:
        """
        从候选表中精筛出 top_m 张最相关的表（异步版本）

        与 sync 版差异：LLM 调用走 ``run_in_executor`` + ``_STAGE2_SEM`` 限流，
        让 event loop 在 vLLM 网络阻塞期间可服务其他请求。

        Args:
            query: 用户自然语言问题
            candidates: 候选表列表（每个元素含 table_name 等字段）
            top_m: 期望保留的表数量

        Returns:
            精筛后的候选表列表（candidates 的子集），按模型给出的相关性顺序排列

        Raises:
            Stage2FilterError: 调用失败、解析失败、或筛出的表名无法在候选中匹配
        """
        if not candidates:
            raise Stage2FilterError("候选表列表为空")
        if top_m <= 0:
            raise Stage2FilterError(f"top_m 必须为正整数，收到 {top_m}")

        # 候选数 ≤ top_m 时无需调用模型，直接返回
        if len(candidates) <= top_m:
            logger.info(
                f"候选表数 {len(candidates)} ≤ top_m {top_m}，跳过精筛"
            )
            return list(candidates)

        # 构建 prompt
        candidates_block = self._build_candidates_block(candidates)
        prompt = self.PROMPT_TEMPLATE.format(
            n=len(candidates),
            m=top_m,
            query=query,
            candidates_block=candidates_block,
        )
        logger.info(
            f"Stage2 调用: model={self.model_name} url={self.api_url} "
            f"n_candidates={len(candidates)} top_m={top_m} prompt_chars={len(prompt)}"
        )

        # 调用模型（异步 + Semaphore 限流）
        # Qwen3 通过 chat_template_kwargs.enable_thinking 控制是否产出 <think> 段
        # vLLM/OpenAI-compat endpoint 支持 extra_body 透传非标准参数
        try:
            async with _STAGE2_SEM:
                completion = await self._client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=self.temperature,
                    extra_body={
                        "chat_template_kwargs": {"enable_thinking": self.enable_thinking}
                    },
                )
        except Exception as e:
            # 把完整错误打到 logger.error，并连同 prompt 头尾片段一起暴露给上层
            logger.error(
                f"Stage2 模型调用失败: model={self.model_name} url={self.api_url} "
                f"err={type(e).__name__}: {e}"
            )
            logger.error(f"Stage2 失败时 prompt 前 200 字符: {prompt[:200]!r}")
            logger.error(f"Stage2 失败时 prompt 末 200 字符: {prompt[-200:]!r}")
            raise Stage2FilterError(
                f"Stage2 模型调用失败 [{type(e).__name__}]: {e}"
            ) from e

        try:
            raw_output = completion.choices[0].message.content or ""
        except (AttributeError, IndexError) as e:
            raise Stage2FilterError(f"模型响应格式异常: {e}") from e

        logger.debug(f"Stage2 原始输出: {raw_output[:300]}")

        # 解析表名
        picked_names = self._parse_table_names(raw_output)

        # 在候选中匹配（保持模型给出的顺序，大小写敏感优先，否则不敏感）
        candidate_index = {c.get("table_name", ""): c for c in candidates}
        candidate_index_lower = {
            (c.get("table_name", "") or "").lower(): c for c in candidates
        }

        kept: List[Dict] = []
        unmatched: List[str] = []
        for name in picked_names:
            if not name:
                continue
            if name in candidate_index:
                kept.append(candidate_index[name])
            elif name.lower() in candidate_index_lower:
                kept.append(candidate_index_lower[name.lower()])
            else:
                unmatched.append(name)

        if not kept:
            raise Stage2FilterError(
                f"模型筛出的表名全部无法匹配候选；picked={picked_names}; "
                f"unmatched={unmatched}"
            )

        if unmatched:
            logger.warning(
                f"Stage2 筛出 {len(picked_names)} 张，{len(unmatched)} 张未匹配候选: "
                f"{unmatched}"
            )

        # 去重 + 截断到 top_m
        seen = set()
        deduped: List[Dict] = []
        for item in kept:
            name = item.get("table_name", "")
            if name in seen:
                continue
            seen.add(name)
            deduped.append(item)
            if len(deduped) >= top_m:
                break

        # 代码层兜底：模型有时只返回 1~2 张（尤其是查询直接点名某张表时），
        # 这会破坏 (N, M) 配比实验的控制变量。
        # 按原候选顺序（即 stage1 相似度顺序）把还没被选中的表补进去，直到长度 == M。
        if len(deduped) < top_m:
            shortfall = top_m - len(deduped)
            filler: List[Dict] = []
            for c in candidates:
                name = c.get("table_name", "")
                if name in seen:
                    continue
                filler.append(c)
                seen.add(name)
                if len(filler) >= shortfall:
                    break
            if filler:
                logger.warning(
                    f"Stage2 模型仅返回 {len(deduped)} 张（目标 {top_m}），"
                    f"按 stage1 相似度顺序补足 {len(filler)} 张: "
                    f"{[c.get('table_name','') for c in filler]}"
                )
                deduped.extend(filler)

        logger.info(
            f"Stage2 精筛: {len(candidates)} → {len(deduped)} 张 "
            f"(目标 {top_m})"
        )
        return deduped
