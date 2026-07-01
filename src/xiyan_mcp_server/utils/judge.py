"""
评价模型（Judge）

对照 NL2SQL 系统生成的 SQL 做客观评价：给定用户问题、生成的 SQL、执行结果，
判断这条 SQL 是否正确回答了用户问题，并给出错误分类。

设计要点（与 stage2_filter 区别）：
- 用途：离线评估（scripts/run_judge_batch.py），不挂 MCP server
- RAG：judge 端**自己独立跑一次单阶段 KNN 检索**，K 比主流程 stage1 N 更大，
       让 judge 能看到"应该选但被测系统漏掉的表"
- Prompt：不告诉 judge "被测系统选了什么表"，避免锚定偏差
         （这些信息已写在 SQL 的 FROM/JOIN 子句里）
- 失败：抛 JudgeError；调用方负责重试或标记为 judge_error
"""
import json
import logging
import re
from typing import List, Dict, Optional

from openai import OpenAI

logger = logging.getLogger(__name__)


class JudgeError(Exception):
    """评价模型调用 / 解析失败"""
    pass


class JudgeModel:
    """
    NL2SQL 评价模型

    用法：
        j = JudgeModel(judge_cfg, retriever=schema_retriever)
        verdict = j.judge(
            nl_query="CPU 使用率最高的 5 个节点",
            generated_sql="SELECT ... FROM sys_cpu_usage ORDER BY ...",
            exec_summary={"success": True, "n_rows": 5, "preview": "..."},
        )
        # verdict = {"correct": True, "category": "correct", "reason": "...",
        #            "judge_model": "...", "judge_top_k": 30, "judge_latency_ms": 8420}
    """

    # 错误类别枚举（写在 prompt 里给模型当 closed-set）
    # 注意：system_failure 不由模型产出，是脚本对"被测系统未生成 SQL"case 的直接标记
    CATEGORIES = [
        "correct",            # SQL 正确回答了问题
        "schema_wrong",       # 用错了表（FROM 子句里的表不对）
        "column_wrong",       # 列用错（表对但列不对）
        "aggregation_wrong",  # 聚合维度错（SUM/COUNT/AVG/GROUP BY 等）
        "filter_wrong",       # WHERE 条件错（漏了/多了/错了限定条件）
        "syntax",             # 语法层错误（SQL 跑不通）
        "other",              # 其它说不清楚的错误
        "system_failure",     # 被测系统未产出 SQL（脚本直接标记，不进 judge 模型）
    ]

    PROMPT_TEMPLATE = """你是数据库 SQL 审查员。你的任务是判断给定 SQL 是否正确回答了用户问题。

# 用户问题
{nl_query}

# 相关数据表 Schema（仅供你参考，可能包含与问题无关的表）
{judge_schema}

# 被审查的 SQL
```sql
{generated_sql}
```

# SQL 执行结果摘要
- 执行状态：{exec_status}
- 返回行数：{n_rows}
- 前几行预览：
{result_preview}

# 判断要求
请仔细分析这条 SQL 是否正确回答了用户问题，并按以下规则输出：

- 关于时间条件：SQL 中硬编码的固定年份（如 2023）请忽略，只要时间跨度和逻辑正确（如"最近 7 天""本月"等范围合理），不视为错误。

1. **correct**（bool）：True 表示 SQL 正确回答了问题；False 表示存在错误
2. **category**（必须从下面 7 类中选一个）：
   - correct：SQL 正确
   - schema_wrong：用错了表（FROM/JOIN 选错了主体表）
   - column_wrong：表对，但列引用错误
   - aggregation_wrong：聚合维度/GROUP BY/HAVING 错误
   - filter_wrong：WHERE 条件错误（漏限定、错限定、范围错）
   - syntax：SQL 语法层错误（导致跑不通）
   - other：其它
3. **reason**（中文，1~2 句）：简要说明判断依据

# 输出格式
**严格输出 JSON 对象**，**不要 markdown 包裹、不要解释、不要 ```json``` 标记**。
形如：{{"correct": true, "category": "correct", "reason": "..."}}

# 你的输出
"""

    def __init__(self, config: dict, retriever=None):
        """
        Args:
            config: judge 配置字典，包含 model_name / api_url / api_key /
                    judge_top_k / temperature / timeout
            retriever: SchemaRetriever 实例，用于 judge 端独立 RAG。
                      要求支持 .retrieve_table_names(query, ..., top_k) 和 .build_sub_schema(names)
        """
        self.model_name = config.get("model_name", "deepseek-ai/DeepSeek-V4-Pro")
        self.api_url = config.get("api_url", "")
        self.api_key = config.get("api_key", "EMPTY") or "EMPTY"
        self.judge_top_k = int(config.get("judge_top_k", 30))
        self.temperature = float(config.get("temperature", 0.0))
        self.timeout = float(config.get("timeout", 90))

        if not self.api_url:
            raise JudgeError("judge.api_url 未配置")

        self.retriever = retriever
        if self.retriever is None:
            raise JudgeError("JudgeModel 需要 retriever 才能跑 judge 端 RAG")

        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.api_url,
            timeout=self.timeout,
        )
        logger.info(
            f"JudgeModel 初始化: model={self.model_name}, url={self.api_url}, "
            f"K={self.judge_top_k}"
        )

    def _retrieve_schema(
        self,
        nl_query: str,
        database: Optional[str] = None,
        system_prefix: Optional[str] = None,
    ) -> str:
        """judge 端单阶段 RAG。强制走单阶段（不调用 stage2 精筛）。"""
        try:
            table_names = self.retriever.retrieve_table_names(
                query=nl_query,
                database=database,
                system_prefix=system_prefix,
                top_k=self.judge_top_k,
            )
            return self.retriever.build_sub_schema(table_names, skip_lazy_load=True)
        except Exception as e:
            raise JudgeError(f"judge 端 RAG 失败: {e}") from e

    def _format_preview(self, preview) -> str:
        """把 result_preview（list of list 或 字符串）格式化成短文本"""
        if preview is None:
            return "（无数据）"
        if isinstance(preview, str):
            return preview[:500] if preview else "（空）"
        if isinstance(preview, list):
            lines = []
            for row in preview[:3]:
                if isinstance(row, list):
                    lines.append(" | ".join(str(c)[:60] for c in row))
                else:
                    lines.append(str(row)[:200])
            return "\n".join(lines) if lines else "（空）"
        return str(preview)[:500]

    def _parse_verdict(self, raw: str) -> Dict:
        """从模型输出中解析 verdict JSON。容错和 stage2 类似"""
        if not raw:
            raise JudgeError("judge 模型输出为空")

        text = raw.strip()
        # 剥 <think>...</think>（防御性，DeepSeek 一般不输出，但有些模型会）
        text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
        text = text.strip()
        # 去 markdown 代码块
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        # 直接解析
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return self._normalize_verdict(parsed)
        except json.JSONDecodeError:
            pass

        # 正则兜底：找第一段 {...}
        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, dict):
                    return self._normalize_verdict(parsed)
            except json.JSONDecodeError as e:
                raise JudgeError(
                    f"verdict JSON 解析失败: {e}; 原始输出={raw[:300]!r}"
                )

        raise JudgeError(f"无法从 judge 输出中提取 JSON; 原始输出={raw[:300]!r}")

    def _normalize_verdict(self, parsed: Dict) -> Dict:
        """规范化 verdict 字段，确保 correct/category/reason 都存在且类型对"""
        correct = parsed.get("correct")
        if not isinstance(correct, bool):
            # 容错：字符串 "true"/"false"
            if isinstance(correct, str):
                correct = correct.strip().lower() in ("true", "yes", "1", "正确")
            else:
                raise JudgeError(
                    f"verdict.correct 不是 bool: {correct!r}; full={parsed}"
                )

        category = parsed.get("category", "other")
        if not isinstance(category, str):
            category = "other"
        category = category.strip().lower()
        if category not in self.CATEGORIES:
            logger.warning(
                f"verdict.category {category!r} 不在枚举内，归为 other"
            )
            category = "other"

        # 一致性兜底：correct=True 但 category 不是 correct → 信 correct=True
        if correct and category != "correct":
            logger.warning(
                f"verdict 不一致: correct=True 但 category={category}，强制改为 correct"
            )
            category = "correct"
        # 反过来：correct=False 但 category=correct → 改成 other
        if not correct and category == "correct":
            logger.warning(
                "verdict 不一致: correct=False 但 category=correct，改为 other"
            )
            category = "other"

        reason = parsed.get("reason", "")
        if not isinstance(reason, str):
            reason = str(reason)
        reason = reason.strip()[:500]   # 限长

        return {
            "correct": correct,
            "category": category,
            "reason": reason,
        }

    def judge(
        self,
        nl_query: str,
        generated_sql: str,
        exec_summary: Dict,
        database: Optional[str] = None,
        system_prefix: Optional[str] = None,
    ) -> Dict:
        """
        评价单条记录

        Args:
            nl_query: 用户自然语言问题
            generated_sql: 被测系统生成的 SQL
            exec_summary: 执行结果摘要，键：
                - success: bool
                - n_rows: int
                - preview: list[list] 或 str
            database / system_prefix: 透传给 retriever 做 judge 端 RAG 的过滤

        Returns:
            verdict 字典，包含 correct / category / reason /
            judge_model / judge_top_k / judge_latency_ms
        """
        import time as _time

        if not nl_query or not generated_sql:
            raise JudgeError("nl_query 和 generated_sql 都不能为空")

        # 1. judge 端独立 RAG
        judge_schema = self._retrieve_schema(
            nl_query, database=database, system_prefix=system_prefix,
        )

        # 2. 构 prompt
        prompt = self.PROMPT_TEMPLATE.format(
            nl_query=nl_query,
            judge_schema=judge_schema,
            generated_sql=generated_sql,
            exec_status="成功" if exec_summary.get("success") else "失败",
            n_rows=exec_summary.get("n_rows", 0),
            result_preview=self._format_preview(exec_summary.get("preview")),
        )

        # 3. 调模型
        t0 = _time.time()
        try:
            completion = self._client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=self.temperature,
            )
        except Exception as e:
            logger.error(
                f"Judge 模型调用失败: model={self.model_name} url={self.api_url} "
                f"err={type(e).__name__}: {e}"
            )
            raise JudgeError(
                f"Judge 模型调用失败 [{type(e).__name__}]: {e}"
            ) from e

        latency_ms = (_time.time() - t0) * 1000

        try:
            raw_output = completion.choices[0].message.content or ""
        except (AttributeError, IndexError) as e:
            raise JudgeError(f"judge 响应格式异常: {e}") from e

        logger.debug(f"Judge 原始输出: {raw_output[:300]}")

        # 4. 解析
        verdict = self._parse_verdict(raw_output)
        verdict.update({
            "judge_model": self.model_name,
            "judge_top_k": self.judge_top_k,
            "judge_latency_ms": round(latency_ms, 2),
        })
        return verdict
