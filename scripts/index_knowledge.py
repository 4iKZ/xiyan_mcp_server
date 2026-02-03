"""
知识库索引初始化脚本

用法:
    python scripts/index_knowledge.py --config config.yml
    python scripts/index_knowledge.py --config config.yml --rebuild
"""
import argparse
import logging
import sys
from pathlib import Path

# 添加项目路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="知识库索引初始化")
    parser.add_argument(
        "--config", "-c",
        required=True,
        help="配置文件路径（YAML）"
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="重建索引（删除已存在的索引）"
    )
    parser.add_argument(
        "--knowledge-dir",
        default=None,
        help="知识库目录（覆盖配置文件中的设置）"
    )
    args = parser.parse_args()
    
    # 加载配置
    import yaml
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"配置文件不存在: {config_path}")
        sys.exit(1)
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 获取各模块配置
    redis_config = config.get("redis", {})
    embedding_config = config.get("embedding", {})
    schema_filter_config = config.get("schema_filter", {})
    
    # 确定知识库目录
    knowledge_dir = args.knowledge_dir or schema_filter_config.get("knowledge_dir", "json")
    
    # 初始化 Redis
    import redis
    redis_client = redis.Redis(
        host=redis_config.get("host", "localhost"),
        port=redis_config.get("port", 6379),
        password=redis_config.get("password") or None,
        decode_responses=False  # 向量需要原始 bytes
    )
    
    # 测试 Redis 连接
    try:
        redis_client.ping()
        logger.info("Redis 连接成功")
    except Exception as e:
        logger.error(f"Redis 连接失败: {e}")
        sys.exit(1)
    
    # 初始化 Embedding 服务
    from xiyan_mcp_server.utils.embedding_service import EmbeddingService
    embedding_service = EmbeddingService(embedding_config)
    
    # 初始化索引管理器
    from xiyan_mcp_server.utils.knowledge_indexer import KnowledgeIndexer
    indexer_config = {
        "index_name": redis_config.get("index_name", "xiyan_schema"),
        "vector_dim": embedding_config.get("vector_dim", 768),
        "knowledge_dir": knowledge_dir
    }
    indexer = KnowledgeIndexer(redis_client, embedding_service, indexer_config)
    
    # 加载知识库
    logger.info(f"加载知识库目录: {knowledge_dir}")
    knowledge = indexer.load_knowledge(knowledge_dir)
    
    if not knowledge:
        logger.error("未找到任何知识库条目")
        sys.exit(1)
    
    # 创建索引
    logger.info("创建 Redis 向量索引...")
    indexer.create_index(drop_existing=args.rebuild)
    
    # 索引数据
    logger.info("索引知识库数据...")
    indexer.index_knowledge(knowledge)
    
    # 显示索引信息
    info = indexer.get_index_info()
    logger.info(f"索引完成！")
    logger.info(f"  索引名称: {indexer.index_name}")
    logger.info(f"  文档数量: {info.get('num_docs', 'N/A')}")
    
    print("\n" + "=" * 50)
    print("✅ 知识库索引创建成功！")
    print("=" * 50)


if __name__ == "__main__":
    main()
