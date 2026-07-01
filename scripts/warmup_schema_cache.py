"""
Schema 缓存预热脚本

将所有表的列信息 + 示例值提前加载并序列化为 JSON 文件，
运行时 GreptimeDBSource.__init__ 直接从缓存加载，跳过所有延迟加载 SQL 查询。

用法:
    # 默认使用 config.yml 中的 database.system
    python scripts/warmup_schema_cache.py --config src/xiyan_mcp_server/config.yml

    # 指定并行度
    python scripts/warmup_schema_cache.py --config src/xiyan_mcp_server/config.yml --workers 20

    # 指定输出路径
    python scripts/warmup_schema_cache.py -c src/xiyan_mcp_server/config.yml -o /tmp/schema_cache.json
"""
import argparse
import logging
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# 添加项目路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="预热 Schema 缓存（列信息 + 示例值）")
    parser.add_argument(
        "--config", "-c",
        default="src/xiyan_mcp_server/config.yml",
        help="配置文件路径（YAML）"
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=10,
        help="并行加载线程数（默认 10）"
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="输出文件路径（默认自动派生为 json/{system}/schema_cache.json）"
    )
    args = parser.parse_args()

    # 加载配置
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"配置文件不存在: {config_path}")
        sys.exit(1)

    import yaml
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    db_config = config["database"]
    system_prefix = db_config.get("system", "")
    if not system_prefix:
        logger.error("未指定 system（database.system），无法确定工作空间")
        sys.exit(1)

    # 创建 engine + GreptimeDBSource
    from xiyan_mcp_server.utils.db_config import DBConfig
    from xiyan_mcp_server.utils.db_util import init_db_conn
    from xiyan_mcp_server.utils.greptimedb_source import GreptimeDBSource

    xiyan_db_config = DBConfig(
        dialect=db_config["dialect"],
        db_name=db_config["database"],
        user_name=db_config["user"],
        db_pwd=db_config["password"],
        db_host=db_config["host"],
        port=db_config["port"],
    )
    engine = init_db_conn(xiyan_db_config)
    source = GreptimeDBSource(
        engine,
        db_name=db_config["database"],
        system_prefix=system_prefix,
    )

    # 确定输出路径
    knowledge_dir = config.get("schema_filter", {}).get("knowledge_dir", "json")
    cache_path = args.output or str(
        Path(knowledge_dir) / system_prefix.lower() / "schema_cache.json"
    )

    # 并行加载所有表的列信息
    tables = list(source._usable_tables)
    logger.info(f"开始预热 {len(tables)} 张表，并行度={args.workers}")
    logger.info(f"缓存输出路径: {cache_path}")

    def load_one(full_table_name):
        """加载单个表的列信息，返回 (table_name, success, error_msg)"""
        parts = full_table_name.split(".", 1)
        if len(parts) != 2:
            return full_table_name, False, f"无效的表名格式: {full_table_name}"
        schema_name, table_name = parts
        try:
            source._load_table_columns(schema_name, table_name)
            return full_table_name, True, None
        except Exception as e:
            return full_table_name, False, str(e)

    success, fail = 0, 0
    failed_tables = []
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(load_one, t): t for t in tables}
        for i, future in enumerate(as_completed(futures), 1):
            table_name, ok, err = future.result()
            if ok:
                success += 1
            else:
                fail += 1
                failed_tables.append((table_name, err))
            if i % 100 == 0 or i == len(tables):
                elapsed = time.time() - t0
                logger.info(
                    f"进度: {i}/{len(tables)} "
                    f"(成功={success}, 失败={fail}, 耗时={elapsed:.1f}s)"
                )

    elapsed = time.time() - t0

    # 保存缓存
    Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
    source._mschema.save(cache_path)
    file_size = Path(cache_path).stat().st_size

    # 统计已填充列信息的表数
    loaded_count = sum(
        1 for t in source._mschema.tables.values() if t.get("fields")
    )

    print(f"\n{'=' * 60}")
    print(f"Schema 缓存预热完成！")
    print(f"  总表数:       {len(tables)}")
    print(f"  成功加载:     {success}")
    print(f"  失败:         {fail}")
    print(f"  已填充列信息: {loaded_count}")
    print(f"  耗时:         {elapsed:.1f}s ({elapsed / 60:.1f}min)")
    print(f"  缓存文件:     {cache_path}")
    print(f"  文件大小:     {file_size / 1024 / 1024:.2f} MB")
    if failed_tables:
        print(f"\n  失败表列表（前 20 个）:")
        for name, err in failed_tables[:20]:
            print(f"    {name}: {err}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
