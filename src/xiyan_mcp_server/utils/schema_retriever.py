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
    
    def retrieve(
        self,
        query: str,
        database: Optional[str] = None,
        top_k: Optional[int] = None
    ) -> List[Dict]:
        """
        检索与查询最相关的表
        
        Args:
            query: 用户问题
            database: 限定数据库（可选）
            top_k: 返回数量（可选，默认使用配置值）
            
        Returns:
            检索结果列表，每个元素包含：
            - table_name: 表名
            - friendly_name: 友好名称
            - description: 描述
            - score: 相似度分数
        """
        from redis.commands.search.query import Query
        
        k = top_k or self.top_k
        
        # 生成查询向量
        query_embedding = self.embedding_service.embed_single(query)
        logger.info(f"生成的查询向量长度: {len(query_embedding)}, 前5个值: {query_embedding[:5]}")
        query_bytes = np.array(query_embedding, dtype=np.float32).tobytes()
        
        # 构建查询
        # 基础查询：KNN 向量搜索
        base_query = f"*=>[KNN {k} @embedding $query_vec AS score]"
        
        # 如果指定了数据库，添加过滤条件
        if database:
            base_query = f"@database:{{{database}}}=>[KNN {k} @embedding $query_vec AS score]"
        
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
        top_k: Optional[int] = None
    ) -> List[str]:
        """
        检索并只返回表名列表
        
        Args:
            query: 用户问题
            database: 限定数据库（可选）
            top_k: 返回数量（可选）
            
        Returns:
            表名列表
        """
        results = self.retrieve(query, database, top_k)
        return [item["table_name"] for item in results]
    
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
        
        # 只复制检索到的表
        for table_name in table_names:
            if table_name in self.mschema.tables:
                table_info = self.mschema.tables[table_name]
                
                # 添加表
                sub_mschema.add_table(
                    table_name,
                    fields=table_info.get('fields', {}),
                    comment=table_info.get('comment', '')
                )
                
                # 复制字段信息
                if 'fields' in table_info:
                    for field_name, field_info in table_info['fields'].items():
                        sub_mschema.tables[table_name]['fields'][field_name] = field_info
        
        sub_schema_str = sub_mschema.to_mschema()
        
        logger.info(
            f"构建 Sub-Schema：{len(table_names)} 个表，"
            f"{len(sub_schema_str)} 字符"
        )
        
        return sub_schema_str
    
    def retrieve_and_build(
        self,
        query: str,
        database: Optional[str] = None
    ) -> Tuple[List[str], str]:
        """
        一站式方法：检索表并构建 Sub-Schema
        
        Args:
            query: 用户问题
            database: 限定数据库（可选）
            
        Returns:
            (表名列表, Sub-Schema 字符串)
        """
        table_names = self.retrieve_table_names(query, database)
        sub_schema = self.build_sub_schema(table_names)
        return table_names, sub_schema
