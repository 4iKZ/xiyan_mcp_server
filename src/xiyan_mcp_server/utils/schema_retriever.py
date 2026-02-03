"""
Schema 检索服务模块

负责语义检索和 Sub-Schema 构建。
"""
import logging
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
        config: dict
    ):
        """
        初始化检索服务
        
        Args:
            redis_client: Redis 客户端实例
            embedding_service: Embedding 服务实例
            mschema: M-Schema 对象（包含完整表结构）
            config: 配置字典，包含：
                - index_name: Redis 索引名称
                - top_k: 返回的表数量
                - score_threshold: 相似度阈值
        """
        self.redis = redis_client
        self.embedding_service = embedding_service
        self.mschema = mschema
        self.index_name = config.get("index_name", "xiyan_schema")
        self.top_k = config.get("top_k", 5)
        self.score_threshold = config.get("score_threshold", 0.6)
    
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
        top_k: Optional[int] = None
    ) -> List[Dict]:
        """
        检索与查询最相关的表
        
        Args:
            query: 用户问题
            database: 限定单个数据库（可选）
            system_prefix: 限定系统前缀（可选，优先于 database）
            top_k: 返回数量（可选，默认使用配置值）
            
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
                .dialect(2)
            )
            
            results = self.redis.ft(self.index_name).search(
                query_obj,
                query_params={"query_vec": query_bytes}
            )
            
            # 解析结果
            retrieved = []
            logger.info(f"Redis 搜索返回了 {len(results.docs)} 个原始文档")
            for doc in results.docs:
                score = float(doc.score) if hasattr(doc, 'score') else 0.0
                # Redis 返回的是距离，转换为相似度（余弦距离：1 - distance）
                similarity = 1 - score
                
                logger.info(f"表: {doc.table_name}, 距离: {score:.4f}, 相似度: {similarity:.4f}")
                
                if similarity >= self.score_threshold:
                    retrieved.append({
                        "table_name": doc.table_name,
                        "friendly_name": getattr(doc, 'friendly_name', ''),
                        "description": getattr(doc, 'description', ''),
                        "database": getattr(doc, 'database', ''),
                        "score": similarity
                    })
            
            logger.info(f"检索到 {len(retrieved)} 个相关表（阈值: {self.score_threshold}）")
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

    def build_sub_schema(self, table_names: List[str]) -> str:
        """
        根据表名列表从完整 M-Schema 中提取 Sub-Schema

        Args:
            table_names: 需要包含的表名列表

        Returns:
            Sub-Schema 字符串（M-Schema 格式）
        """
        if not table_names:
            # 如果没有检索到任何表，返回完整 Schema
            logger.warning("未检索到相关表，使用完整 Schema")
            return self.mschema.to_mschema()

        # 创建子 MSchema
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

                # 添加表（使用原始大小写的表名）
                sub_mschema.add_table(
                    actual_table_name,
                    fields=table_info.get('fields', {}),
                    comment=table_info.get('comment', '')
                )

                # 复制字段信息
                if 'fields' in table_info:
                    for field_name, field_info in table_info['fields'].items():
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
        system_prefix: Optional[str] = None
    ) -> Tuple[List[str], str]:
        """
        一站式方法：检索表并构建 Sub-Schema
        
        Args:
            query: 用户问题
            database: 限定数据库（可选）
            system_prefix: 限定系统前缀（可选）
            
        Returns:
            (表名列表, Sub-Schema 字符串)
        """
        table_names = self.retrieve_table_names(query, database, system_prefix)
        sub_schema = self.build_sub_schema(table_names)
        return table_names, sub_schema
