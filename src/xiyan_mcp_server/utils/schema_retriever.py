"""
Schema 检索服务模块

负责语义检索和 Sub-Schema 构建。
"""
import json
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)


class SchemaRetriever:
    """
    Schema 语义检索服务
    
    负责：
    1. 根据用户问题检索最相关的表
    2. 从完整 M-Schema 中提取 Sub-Schema
    """
    
    def __init__(
        self,
        redis_client,
        embedding_service,
        mschema,
        config: dict,
        db_source=None,
        stage2_filter=None,
    ):
        """
        初始化检索服务

        Args:
            redis_client: Redis 客户端实例
            embedding_service: Embedding 服务实例
            mschema: M-Schema 对象（包含完整表结构）
            config: 配置字典，包含：
                - index_name: Redis 索引名称
                - top_k: 返回的表数量（单阶段模式）
                - score_threshold: 相似度阈值
                - stage2: 二级筛选配置，含 enabled / stage1_top_n / stage2_top_m
            db_source: 数据库源对象（可选，用于延迟加载列信息）
            stage2_filter: Stage2Filter 实例（可选，启用二级筛选时必须传入）
        """
        self.redis = redis_client
        self.embedding_service = embedding_service
        self.mschema = mschema
        self.db_source = db_source
        self.index_name = config.get("index_name", "xiyan_schema")
        self.top_k = config.get("top_k", 5)
        self.score_threshold = config.get("score_threshold", 0.6)

        # 二级筛选配置
        stage2_cfg = config.get("stage2", {}) or {}
        self.stage2_enabled = bool(stage2_cfg.get("enabled", False))
        self.stage1_top_n = int(stage2_cfg.get("stage1_top_n", 20))
        self.stage2_top_m = int(stage2_cfg.get("stage2_top_m", 5))
        self.stage2_filter = stage2_filter
        if self.stage2_enabled and self.stage2_filter is None:
            logger.warning(
                "schema_filter.stage2.enabled=true 但未注入 Stage2Filter 实例，"
                "二级筛选将被强制关闭"
            )
            self.stage2_enabled = False

        # 加载知识库表描述（供 build_sub_schema 拼 XiYan 和 judge 的 schema prompt）
        # 使用 embedding_content（完整中文描述段落，噪声也注入在此字段）
        self.kb_table_descriptions: Dict[str, str] = {}
        kb_dir = config.get("knowledge_dir", "json")
        system_prefix = config.get("system_prefix", "")
        kb_path = Path(kb_dir) / system_prefix.lower() if system_prefix else Path(kb_dir)
        if kb_path.is_dir():
            for kf in kb_path.glob("*.json"):
                try:
                    for t in json.loads(kf.read_text(encoding="utf-8")):
                        ec = t.get("embedding_content", "")
                        if ec:
                            self.kb_table_descriptions[t["table_name"].lower()] = ec
                except Exception as e:
                    logger.warning(f"加载知识库文件失败 {kf}: {e}")
        if self.kb_table_descriptions:
            logger.info(f"知识库表描述已加载: {len(self.kb_table_descriptions)} 张表")
    
    def _get_matched_database_tags(self, prefix: str) -> List[str]:
        """
        从 Redis 获取所有匹配前缀的 database 标签

        注意：Redis TAG 字段存储的是小写值（如 cockroachdb_metrics），
        而配置中的 system_prefix 可能是首字母大写（如 CockroachDB）。
        这里使用不区分大小写的前缀匹配。
        """
        try:
            # 使用 FT.TAGVALS 获取所有 database 字段的值
            all_tags = self.redis.execute_command("FT.TAGVALS", self.index_name, "database")
            if not all_tags:
                return []

            # 使用不区分大小写的前缀匹配
            prefix_lower = prefix.lower()
            matched = [
                tag.decode() if isinstance(tag, bytes) else tag
                for tag in all_tags
                if (tag.decode() if isinstance(tag, bytes) else tag).lower().startswith(prefix_lower)
            ]
            logger.info(f"系统前缀 '{prefix}' (小写: {prefix_lower}) 匹配到标签: {matched}")
            return matched
        except Exception as e:
            logger.warning(f"获取标签失败: {e}")
            return []

    def retrieve(
        self,
        query: str,
        database: Optional[str] = None,
        system_prefix: Optional[str] = None,
        top_k: Optional[int] = None,
        ignore_threshold: bool = False,
    ) -> List[Dict]:
        """
        检索与查询最相关的表

        Args:
            query: 用户问题
            database: 限定单个数据库（可选）
            system_prefix: 限定系统前缀（可选，优先于 database）
            top_k: 返回数量（可选，默认使用配置值）
            ignore_threshold: 是否忽略 score_threshold（默认 False）。
                stage2 粗召回时应传 True，让阈值不干扰严格的 N 张召回，
                精筛交给 stage2 模型负责。

        Returns:
            检索结果列表
        """
        from redis.commands.search.query import Query

        k = top_k or self.top_k

        # 生成查询向量
        query_embedding = self.embedding_service.embed_single(query)
        query_bytes = np.array(query_embedding, dtype=np.float32).tobytes()

        # 构建过滤条件
        filter_str = "*"
        if system_prefix:
            matched_tags = self._get_matched_database_tags(system_prefix)
            if matched_tags:
                # 使用 | 分隔多个标签实现并集查询
                tags_joined = " | ".join(matched_tags)
                filter_str = f"@database:{{{tags_joined}}}"
        elif database:
            filter_str = f"@database:{{{database}}}"

        # 构建最终 KNN 查询
        base_query = f"({filter_str})=>[KNN {k} @embedding $query_vec AS score]"

        # 执行查询
        try:
            query_obj = (
                Query(base_query)
                .return_fields("table_name", "friendly_name", "description", "database", "score")
                .sort_by("score")
                .paging(0, k)   # ← 关键：Redis Query 默认 LIMIT 0 10，会把 KNN 返回的 k 条结果截到 10 条
                .dialect(2)
            )

            results = self.redis.ft(self.index_name).search(
                query_obj,
                query_params={"query_vec": query_bytes}
            )

            # 解析结果
            retrieved = []
            effective_threshold = 0.0 if ignore_threshold else self.score_threshold
            logger.info(
                f"Redis 搜索返回 {len(results.docs)} 个原始文档（阈值={effective_threshold}"
                f"{', ignore_threshold=True' if ignore_threshold else ''}）"
            )
            for doc in results.docs:
                score = float(doc.score) if hasattr(doc, 'score') else 0.0
                # Redis 返回的是距离，转换为相似度（余弦距离：1 - distance）
                similarity = 1 - score

                logger.debug(f"表: {doc.table_name}, 距离: {score:.4f}, 相似度: {similarity:.4f}")

                if similarity >= effective_threshold:
                    retrieved.append({
                        "table_name": doc.table_name,
                        "friendly_name": getattr(doc, 'friendly_name', ''),
                        "description": getattr(doc, 'description', ''),
                        "database": getattr(doc, 'database', ''),
                        "score": similarity
                    })

            logger.info(f"检索到 {len(retrieved)} 个相关表（阈值: {effective_threshold}）")
            for item in retrieved:
                logger.debug(f"  - {item['table_name']}: {item['score']:.3f}")

            return retrieved

        except Exception as e:
            logger.error(f"检索失败: {e}")
            # 降级：返回空列表，使用完整 Schema
            return []

    def retrieve_table_names(
        self,
        query: str,
        database: Optional[str] = None,
        system_prefix: Optional[str] = None,
        top_k: Optional[int] = None
    ) -> List[str]:
        """
        检索并返回带前缀的表名列表
        
        Args:
            query: 用户问题
            database: 限定数据库（可选）
            system_prefix: 限定系统前缀（可选）
            top_k: 返回数量（可选）
            
        Returns:
            带前缀的表名列表 (e.g., ["sundb_metrics.sys_cpu_usage"])
        """
        results = self.retrieve(query, database, system_prefix, top_k)
        # 使用 database.table_name 格式，与 GreptimeDBSource 中的 mschema 保持一致
        # 注意：将 database 字段转换为小写，因为数据库中的 schema 名是小写的
        return [f"{item['database'].lower()}.{item['table_name']}" for item in results]

    def build_sub_schema(self, table_names: List[str], skip_lazy_load: bool = False) -> str:
        """
        根据表名列表从完整 M-Schema 中提取 Sub-Schema

        Args:
            table_names: 需要包含的表名列表
            skip_lazy_load: True 时不触发 GreptimeDB 列信息延迟加载，改用
                            kb_table_descriptions 拼纯描述文本（judge 端使用）。

        Returns:
            Sub-Schema 字符串
        """
        if not table_names:
            logger.warning("未检索到相关表，使用完整 Schema")
            return self.mschema.to_mschema()

        # ── judge 端快速路径：跳过 DB，全局列说明 + KB 描述 ──
        if skip_lazy_load:
            COLUMN_LEGEND = (
                "【通用列说明】cockroach_metrics 下的表为 Prometheus 指标格式，列结构高度统一：\n"
                "- 所有表都有：greptime_timestamp (TIMESTAMP, 采集时间), greptime_value (FLOAT, 指标值),\n"
                "  instance (STRING, 采集实例地址，形如 IP:port), job (STRING, 采集任务名)\n"
                "- 99.7% 的表有：node_id (STRING, CockroachDB 节点 ID)\n"
                "- 约 30% 的表额外有：store (STRING, 存储 ID)\n"
                "- 约 6% 的表额外有：le (FLOAT, 直方图桶上界)\n"
            )
            kb = getattr(self, 'kb_table_descriptions', {})
            lines = [COLUMN_LEGEND]
            for i, table_name in enumerate(table_names, 1):
                short = table_name.split('.', 1)[-1] if '.' in table_name else table_name
                ec = kb.get(short.lower(), '')
                line = f"{i}. {table_name}"
                if ec:
                    ec_clean = ec.replace('\n', ' ').strip()
                    line += f"\n   {ec_clean}"
                lines.append(line)
            result = "\n".join(lines)
            logger.info(
                f"构建 Judge Sub-Schema（全局列说明 + KB 描述, skip_lazy_load）："
                f"{len(table_names)} 张表, {len(result)} 字符"
            )
            return result

        # ── 主流程路径：完整 mschema（含列信息 + 延迟加载）──
        from .db_mschema import MSchema

        sub_mschema = MSchema(
            db_id=self.mschema.db_id,
            schema=self.mschema.schema
        )

        # 创建大小写不敏感的映射表
        mschema_tables_lower = {k.lower(): k for k in self.mschema.tables.keys()}

        # 只复制检索到的表（使用大小写不敏感匹配）
        matched_count = 0
        for table_name in table_names:
            # 使用小写进行匹配
            table_name_lower = table_name.lower()
            if table_name_lower in mschema_tables_lower:
                # 获取原始大小写的表名
                actual_table_name = mschema_tables_lower[table_name_lower]
                table_info = self.mschema.tables[actual_table_name]
                matched_count += 1

                # ✨ 延迟加载：如果表的字段为空，尝试加载列信息
                fields = table_info.get('fields', {})
                if not fields and self.db_source and hasattr(self.db_source, '_load_table_columns'):
                    try:
                        # 解析 schema 和 table 名称
                        parts = actual_table_name.split('.', 1)
                        if len(parts) == 2:
                            schema_name, table_name_only = parts
                            logger.info(f"延迟加载表列信息: {actual_table_name}")
                            self.db_source._load_table_columns(schema_name, table_name_only)
                            # 重新获取表信息（现在应该有字段了）
                            table_info = self.mschema.tables[actual_table_name]
                            fields = table_info.get('fields', {})
                            logger.info(f"延迟加载完成: {actual_table_name}, {len(fields)} 个字段")
                    except Exception as e:
                        logger.warning(f"延迟加载失败 {actual_table_name}: {e}")

                # 拼表注释：优先 mschema 已有 comment，否则从 KB embedding_content 截取
                comment = table_info.get('comment', '')
                if not comment:
                    short = actual_table_name.split('.', 1)[-1] if '.' in actual_table_name else actual_table_name
                    ec = self.kb_table_descriptions.get(short.lower(), '')
                    if ec:
                        # embedding_content 格式："表名: xxx。名称: xxx。描述: xxx。业务含义: xxx。"
                        # 去掉开头重复的"表名: "，截 250 字符
                        ec_clean = ec.replace('\n', ' ').strip()
                        if ec_clean.startswith('表名:'):
                            # 跳过"表名: xxx。"，从"名称:"或"描述:"开始
                            first_period = ec_clean.find('。')
                            if first_period > 0:
                                ec_clean = ec_clean[first_period+1:].strip()
                        comment = ec_clean

                # 添加表（使用原始大小写的表名）
                sub_mschema.add_table(
                    actual_table_name,
                    fields=fields,
                    comment=comment
                )

                # 复制字段信息
                if fields:
                    for field_name, field_info in fields.items():
                        sub_mschema.tables[actual_table_name]['fields'][field_name] = field_info

        sub_schema_str = sub_mschema.to_mschema()

        logger.info(
            f"构建 Sub-Schema：{matched_count}/{len(table_names)} 个表匹配成功，"
            f"{len(sub_schema_str)} 字符"
        )

        return sub_schema_str
    
    def retrieve_and_build(
        self,
        query: str,
        database: Optional[str] = None,
        system_prefix: Optional[str] = None,
        stage2_enabled: Optional[bool] = None,
        stage1_top_n: Optional[int] = None,
        stage2_top_m: Optional[int] = None,
    ) -> Tuple[List[str], str, Dict]:
        """
        一站式方法：检索表并构建 Sub-Schema

        Args:
            query: 用户问题
            database: 限定数据库（可选）
            system_prefix: 限定系统前缀（可选）
            stage2_enabled: 临时覆盖配置中的 stage2.enabled（None 表示用配置默认值）
            stage1_top_n: 临时覆盖配置中的 stage2.stage1_top_n
            stage2_top_m: 临时覆盖配置中的 stage2.stage2_top_m

        Returns:
            (最终表名列表, Sub-Schema 字符串, meta)
            meta 包含本次检索的元数据，便于追踪：
              - stage2_enabled: 本次是否走了二级筛选
              - stage1_top_n / stage2_top_m: 本次实际使用的 N、M
              - stage1_tables: 向量检索召回的表名列表
              - stage2_tables: 模型精筛保留的表名列表（仅二级筛选启用时）
              - stage2_model: 精筛使用的模型名（仅二级筛选启用时）
        """
        # 解析本次调用的 stage2 参数（运行时覆盖优先）
        use_stage2 = self.stage2_enabled if stage2_enabled is None else bool(stage2_enabled)
        n = self.stage1_top_n if stage1_top_n is None else int(stage1_top_n)
        m = self.stage2_top_m if stage2_top_m is None else int(stage2_top_m)

        meta: Dict = {
            "stage2_enabled": use_stage2,
            "stage1_top_n": n if use_stage2 else None,
            "stage2_top_m": m if use_stage2 else None,
            "stage1_tables": [],
            "stage2_tables": [] if use_stage2 else None,
            "stage2_model": None,
        }

        if use_stage2 and self.stage2_filter is None:
            # 防御性：如果运行时强行打开但没有筛选器，明确报错
            raise RuntimeError(
                "请求启用 stage2，但 SchemaRetriever 未注入 Stage2Filter 实例"
            )

        if use_stage2:
            # ── 两阶段路径 ──
            # Stage 1: 向量粗召回 N 张
            # 注意：粗召回阶段忽略 score_threshold，严格返回 N 张候选，
            # 让阈值过滤不干扰 (N, M) 配比实验的控制变量。
            # 精度问题交给 Stage 2 模型负责。
            stage1_results = self.retrieve(
                query, database, system_prefix, top_k=n,
                ignore_threshold=True,
            )
            stage1_table_names = [
                f"{item['database'].lower()}.{item['table_name']}"
                for item in stage1_results
            ]
            meta["stage1_tables"] = stage1_table_names

            # 给候选表补充 embedding_content（KB 描述全文），供 stage2 精筛时参考
            for item in stage1_results:
                if "embedding_content" not in item:
                    ec = self.kb_table_descriptions.get(item.get("table_name", "").lower(), "")
                    if ec:
                        item["embedding_content"] = ec

            # Stage 2: 模型精筛到 M 张
            stage2_results = self.stage2_filter.filter(
                query=query,
                candidates=stage1_results,
                top_m=m,
            )
            final_table_names = [
                f"{item['database'].lower()}.{item['table_name']}"
                for item in stage2_results
            ]
            meta["stage2_tables"] = final_table_names
            meta["stage2_model"] = getattr(self.stage2_filter, "model_name", None)
        else:
            # ── 单阶段路径（原有行为）──
            final_table_names = self.retrieve_table_names(
                query, database, system_prefix
            )
            meta["stage1_tables"] = final_table_names

        sub_schema = self.build_sub_schema(final_table_names)
        return final_table_names, sub_schema, meta
