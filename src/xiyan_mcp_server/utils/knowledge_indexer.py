"""
知识库索引管理模块

负责读取 JSON 知识库、创建 Redis 向量索引和索引数据。
"""
import json
import logging
import os
from pathlib import Path
from typing import List, Dict, Optional
import numpy as np

logger = logging.getLogger(__name__)


class KnowledgeIndexer:
    """
    知识库索引管理器
    
    负责：
    1. 读取 json/ 目录下的知识库文件
    2. 创建 Redis 向量索引
    3. 将知识库条目索引到 Redis
    """
    
    def __init__(self, redis_client, embedding_service, config: dict):
        """
        初始化索引管理器
        
        Args:
            redis_client: Redis 客户端实例
            embedding_service: Embedding 服务实例
            config: 配置字典，包含：
                - index_name: 索引名称
                - vector_dim: 向量维度
                - knowledge_dir: 知识库目录
        """
        self.redis = redis_client
        self.embedding_service = embedding_service
        self.index_name = config.get("index_name", "xiyan_schema")
        self.vector_dim = config.get("vector_dim", 768)
        self.knowledge_dir = config.get("knowledge_dir", "json")
        self.prefix = f"{self.index_name}:"
    
    def load_knowledge(self, json_dir: Optional[str] = None) -> List[Dict]:
        """
        加载知识库目录下所有 JSON 文件
        
        Args:
            json_dir: 知识库目录路径，默认使用配置中的目录
            
        Returns:
            知识库条目列表，每个条目包含 database 字段标识来源
        """
        json_dir = json_dir or self.knowledge_dir
        knowledge_items = []
        
        # 确保路径存在
        json_path = Path(json_dir)
        if not json_path.exists():
            logger.warning(f"知识库目录不存在: {json_dir}")
            return knowledge_items
        
        # 遍历所有 *_knowledge.json 文件
        for json_file in json_path.glob("*_knowledge.json"):
            try:
                # 从文件名提取数据库名
                # 例如: sundb_metrics_knowledge.json -> sundb_metrics
                db_name = json_file.stem.replace("_knowledge", "")
                
                with open(json_file, 'r', encoding='utf-8') as f:
                    items = json.load(f)
                
                # 为每个条目添加 database 标识
                for item in items:
                    item['database'] = db_name
                    knowledge_items.append(item)
                
                logger.info(f"加载知识库: {json_file.name}，共 {len(items)} 个表")
                
            except Exception as e:
                logger.error(f"加载知识库文件失败 {json_file}: {e}")
        
        logger.info(f"共加载 {len(knowledge_items)} 个知识库条目")
        return knowledge_items
    
    def create_index(self, drop_existing: bool = False):
        """
        创建 Redis 向量索引
        
        Args:
            drop_existing: 是否删除已存在的索引
        """
        from redis.commands.search.field import (
            TextField,
            TagField,
            VectorField
        )
        try:
            from redis.commands.search.indexDefinition import IndexDefinition, IndexType
        except ImportError:
            # 兼容旧版本或不同版本的 redis-py
            from redis.commands.search.index_definition import IndexDefinition, IndexType
        
        # 检查索引是否存在
        try:
            self.redis.ft(self.index_name).info()
            if drop_existing:
                logger.info(f"删除已存在的索引: {self.index_name}")
                self.drop_index()
            else:
                logger.info(f"索引已存在: {self.index_name}")
                return
        except Exception:
            # 索引不存在，继续创建
            pass
        
        # 定义索引 Schema
        schema = (
            TextField("table_name"),
            TextField("friendly_name"),
            TextField("description"),
            TextField("business_meaning"),
            TagField("category"),
            TagField("database"),
            VectorField(
                "embedding",
                "FLAT",  # 使用 FLAT 算法（适合小规模数据）
                {
                    "TYPE": "FLOAT32",
                    "DIM": self.vector_dim,
                    "DISTANCE_METRIC": "COSINE"
                }
            )
        )
        
        # 创建索引
        definition = IndexDefinition(
            prefix=[self.prefix],
            index_type=IndexType.HASH
        )
        
        try:
            self.redis.ft(self.index_name).create_index(
                schema,
                definition=definition
            )
            logger.info(f"创建索引成功: {self.index_name}")
        except Exception as e:
            logger.error(f"创建索引失败: {e}")
            raise
    
    def index_knowledge(self, knowledge: List[Dict]):
        """
        将知识库条目索引到 Redis
        
        Args:
            knowledge: 知识库条目列表
        """
        if not knowledge:
            logger.warning("没有知识库条目需要索引")
            return
        
        # 提取 embedding_content 字段用于向量化
        texts = [
            item.get("embedding_content", item.get("description", ""))
            for item in knowledge
        ]
        
        # 批量生成向量
        logger.info(f"正在生成 {len(texts)} 个文本的向量...")
        embeddings = self.embedding_service.embed(texts)
        
        # 存储到 Redis
        pipeline = self.redis.pipeline()
        
        for i, (item, embedding) in enumerate(zip(knowledge, embeddings)):
            # 构建 Redis key
            key = f"{self.prefix}{item['database']}:{item['table_name']}"
            
            # 转换向量为 bytes
            embedding_bytes = np.array(embedding, dtype=np.float32).tobytes()
            
            # 存储 Hash
            pipeline.hset(
                key,
                mapping={
                    "table_name": item.get("table_name", ""),
                    "friendly_name": item.get("friendly_name", ""),
                    "description": item.get("description", ""),
                    "business_meaning": item.get("business_meaning", ""),
                    "category": item.get("category", ""),
                    "database": item.get("database", ""),
                    "embedding": embedding_bytes
                }
            )
        
        pipeline.execute()
        logger.info(f"索引完成，共 {len(knowledge)} 个条目")
    
    def drop_index(self):
        """删除索引"""
        try:
            self.redis.ft(self.index_name).dropindex(delete_documents=True)
            logger.info(f"索引已删除: {self.index_name}")
        except Exception as e:
            logger.warning(f"删除索引失败: {e}")
    
    def index_exists(self) -> bool:
        """检查索引是否存在"""
        try:
            self.redis.ft(self.index_name).info()
            return True
        except Exception:
            return False
    
    def get_index_info(self) -> Dict:
        """获取索引信息"""
        try:
            return self.redis.ft(self.index_name).info()
        except Exception:
            return {}
