import argparse
import logging
import os
import signal
import sys
import time
import threading

import yaml  # 添加yaml库导入
from mcp.server import FastMCP
from mcp.types import TextContent

from .database_env import DataBaseEnv
from .utils.db_config import DBConfig
from .utils.db_source import HITLSQLDatabase
from .utils.db_util import init_db_conn
from .utils.file_util import extract_sql_from_qwen
from .utils.llm_util import call_openai_sdk


def create_db_source(db_engine, dialect: str, db_name: str = '', system_prefix: str = ''):
    """根据方言创建合适的数据源"""
    if dialect.lower() in ('greptimedb', 'greptimedb_mysql'):
        from .utils.greptimedb_source import GreptimeDBSource
        return GreptimeDBSource(db_engine, db_name=db_name, system_prefix=system_prefix)
    else:
        return HITLSQLDatabase(db_engine)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("xiyan_mcp_server")


# 优雅关闭机制
_shutting_down = False
_shutdown_lock = threading.Lock()


def is_shutting_down():
    """检查服务器是否正在关闭"""
    with _shutdown_lock:
        return _shutting_down


def signal_handler(sig, frame):
    """
    处理退出信号，优雅地关闭服务器

    实现步骤：
    1. 设置关闭标志，阻止新请求
    2. 等待活跃请求完成（最多5秒）
    3. 清理资源
    4. 退出
    """
    global _shutting_down

    with _shutdown_lock:
        if _shutting_down:
            logger.warning("关闭信号已被处理，正在等待清理完成...")
            return  # 直接返回，让第一次信号处理完成清理
        _shutting_down = True

    logger.info(f"收到信号 {sig}，开始优雅关闭服务器...")

    # 等待活跃请求完成（最多5秒）
    graceful_shutdown_timeout = 5
    logger.info(f"等待 {graceful_shutdown_timeout} 秒让活跃请求完成...")

    for i in range(graceful_shutdown_timeout):
        time.sleep(1)
        remaining = graceful_shutdown_timeout - i - 1
        if remaining > 0:
            logger.debug(f"剩余等待时间: {remaining} 秒")

    logger.info("开始清理资源...")

    # 清理数据库引擎
    global _db_engine
    if _db_engine is not None:
        try:
            _db_engine.dispose()
            logger.info("数据库连接池已释放")
        except Exception as e:
            logger.error(f"释放数据库连接时出错: {e}")

    # 清理 Redis 连接（如果启用）
    if schema_filter_enabled:
        try:
            global _redis_client
            if _redis_client is not None:
                _redis_client.close()
                logger.info("Redis 连接已关闭")
        except Exception as e:
            logger.error(f"关闭 Redis 连接时出错: {e}")

    logger.info("服务器已安全关闭")
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def get_yml_config():
    config_path = os.getenv(
        "YML", os.path.join(os.path.dirname(__file__), "config.yml")
    )
    logger.info(f"Loading configuration from {config_path}")
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
        return config
    except FileNotFoundError:
        logger.error(f"Configuration file {config_path} not found.")
        raise
    except yaml.YAMLError as exc:
        logger.error(f"Error parsing configuration file {config_path}: {exc}")
        raise


def validate_config(config: dict) -> None:
    """验证配置文件的必需字段

    Args:
        config: 配置字典

    Raises:
        ValueError: 当缺少必需字段时
    """
    # 定义 SQLite 以外的数据库所需的必需字段
    db_required_fields = ["dialect", "host", "port", "user", "password", "database"]

    # 检查 model section
    if "model" not in config:
        raise ValueError("配置文件缺少必需的 section: [model]")

    model_config = config["model"]
    for field in ["name", "key", "url"]:
        if field not in model_config or model_config[field] is None:
            raise ValueError(f"配置文件 [model] 缺少必需字段: {field}")

    # 检查 database section（SQLite 除外）
    if "database" not in config:
        raise ValueError("配置文件缺少必需的 section: [database]")

    db_config = config["database"]
    dialect = db_config.get("dialect", "").lower()

    # SQLite 只需要 dialect 字段
    if dialect != "sqlite":
        for field in db_required_fields:
            if field not in db_config or db_config[field] is None:
                raise ValueError(f"配置文件 [database] 缺少必需字段: {field}")


def get_xiyan_config(db_config):
    dialect = db_config.get("dialect", "mysql")

    if dialect.lower() == "sqlite":
        xiyan_db_config = DBConfig(
            dialect=dialect,
            db_path=db_config.get("db_path"),
        )
    else:
        xiyan_db_config = DBConfig(
            dialect=dialect,
            db_name=db_config["database"],
            user_name=db_config["user"],
            db_pwd=db_config["password"],
            db_host=db_config["host"],
            port=db_config["port"],
        )
    return xiyan_db_config


global_config = get_yml_config()
validate_config(global_config)
mcp_config = global_config.get("mcp", {})
model_config = global_config["model"]
global_db_config = global_config["database"]
global_system_prefix = global_db_config.get("system", "")
global_xiyan_db_config = get_xiyan_config(global_db_config)
dialect = global_db_config.get("dialect", "mysql")
# 规范化 dialect 作为 URL scheme（下划线不允许在 URL scheme 中）
dialect_scheme = dialect.replace("_", "-")

# 全局数据库引擎单例（避免连接池泄漏）
_db_engine = None
_db_engine_lock = threading.Lock()  # 立即初始化，避免竞态条件

def get_db_engine():
    """获取全局数据库引擎单例，避免每次请求创建新引擎导致连接池泄漏"""
    global _db_engine
    if _db_engine is None:
        with _db_engine_lock:
            # Double-check locking
            if _db_engine is None:
                _db_engine = init_db_conn(global_xiyan_db_config)
                logger.info("全局数据库引擎已初始化")
    return _db_engine

# Schema 过滤配置
schema_filter_config = global_config.get("schema_filter", {})
schema_filter_enabled = schema_filter_config.get("enabled", False)
embedding_config = global_config.get("embedding", {})
redis_config = global_config.get("redis", {})

# 全局 Redis 客户端单例（线程安全）
_redis_client = None
_redis_client_lock = threading.Lock()  # 立即初始化，避免竞态条件

def get_redis_client():
    """获取全局 Redis 客户端单例（线程安全）"""
    global _redis_client
    if _redis_client is None:
        with _redis_client_lock:
            if _redis_client is None:
                try:
                    import redis
                    _redis_client = redis.Redis(
                        host=redis_config.get("host", "localhost"),
                        port=redis_config.get("port", 6379),
                        password=redis_config.get("password") or None,
                        decode_responses=False
                    )
                    _redis_client.ping()
                    logger.info("Redis 客户端已初始化")
                except Exception as e:
                    logger.error(f"Redis 连接失败: {e}")
                    raise
    return _redis_client

# 全局 Embedding 服务单例（线程安全）
_embedding_service = None
_embedding_service_lock = threading.Lock()  # 立即初始化，避免竞态条件

def get_embedding_service():
    """获取全局 Embedding 服务单例（线程安全）"""
    global _embedding_service
    if _embedding_service is None:
        with _embedding_service_lock:
            if _embedding_service is None:
                try:
                    from .utils.embedding_service import EmbeddingService
                    _embedding_service = EmbeddingService(embedding_config)
                    logger.info(f"Embedding 模型已加载: {embedding_config.get('model', 'default')}")
                except Exception as e:
                    logger.error(f"Embedding 服务初始化失败: {e}")
                    raise
    return _embedding_service

# Schema 检索器配置（延迟初始化）
_retriever_config = {
    "index_name": redis_config.get("index_name", "xiyan_schema"),
    "top_k": schema_filter_config.get("top_k", 5),
    "score_threshold": schema_filter_config.get("score_threshold", 0.6)
}

# 初始化 Schema 检索器（如果启用）
schema_retriever = None
_schema_retriever_lock = threading.Lock()  # 立即初始化，避免竞态条件

def get_schema_retriever(db_source):
    """获取全局 Schema 检索器单例（线程安全，避免重复初始化检查）"""
    global schema_retriever
    if schema_retriever is None:
        with _schema_retriever_lock:
            if schema_retriever is None:
                from .utils.schema_retriever import SchemaRetriever
                schema_retriever = SchemaRetriever(
                    get_redis_client(),
                    get_embedding_service(),
                    db_source.mschema,
                    _retriever_config,
                    db_source=db_source
                )
                logger.info("Schema 检索器已初始化")
    return schema_retriever

# Schema 缓存配置
_SCHEMA_CACHE_TTL = 600  # TTL: 10 分钟（秒）
_schema_cache = {}  # {cache_key: (schema_str, expire_time)}
_schema_cache_lock = threading.Lock()

def _get_schema_cache_key(db_name: str, system_prefix: str) -> str:
    """生成 Schema 缓存键"""
    return f"{db_name}:{system_prefix}"

def _is_cache_entry_valid(cache_entry, current_time: float) -> bool:
    """检查缓存条目是否有效"""
    if cache_entry is None:
        return False
    schema_str, expire_time = cache_entry
    return current_time < expire_time

def _get_schema_from_cache(cache_key: str) -> str:
    """从缓存获取 Schema（线程安全，检查 TTL）"""
    with _schema_cache_lock:
        if cache_key in _schema_cache:
            cache_entry = _schema_cache[cache_key]
            current_time = time.time()

            if _is_cache_entry_valid(cache_entry, current_time):
                schema_str, expire_time = cache_entry
                logger.debug(f"返回缓存的 Schema (剩余 {int(expire_time - current_time)} 秒)")
                return schema_str
            else:
                # 缓存已过期，删除
                del _schema_cache[cache_key]
                logger.debug(f"Schema 缓存已过期: {cache_key}")
        return None

def _set_schema_to_cache(cache_key: str, schema_str: str) -> None:
    """将 Schema 存入缓存（线程安全，设置过期时间）"""
    current_time = time.time()
    expire_time = current_time + _SCHEMA_CACHE_TTL

    with _schema_cache_lock:
        _schema_cache[cache_key] = (schema_str, expire_time)
        logger.debug(f"Schema 已缓存 (TTL: {_SCHEMA_CACHE_TTL} 秒)")

# 测试 Redis 连接（仅在启用时）
if schema_filter_enabled:
    try:
        get_redis_client()  # 测试连接
        logger.info("Schema 过滤已启用，Redis 连接测试成功")
    except Exception as e:
        logger.warning(f"Schema 过滤初始化失败，将使用完整 Schema: {e}")
        schema_filter_enabled = False

logger.info("正在初始化 FastMCP...")
mcp = FastMCP("xiyan", **mcp_config)
logger.info("FastMCP 初始化完成")

logger.info("正在注册资源和工具...")
@mcp.resource(
    dialect_scheme
    + "://"
    + (
        global_db_config.get("db_path", "")
        if dialect.lower() == "sqlite"
        else "/" + global_db_config.get("database", "")
    )
)
def read_resource() -> str:
    """读取数据库 Schema，支持 TTL 缓存和表数量限制"""
    if is_shutting_down():
        return "服务器正在关闭，暂时不接受新请求"

    # 检查缓存（带 TTL 检查）
    cache_key = _get_schema_cache_key(
        global_db_config.get("database", ""),
        global_system_prefix
    )

    # 尝试从缓存获取
    cached_schema = _get_schema_from_cache(cache_key)
    if cached_schema is not None:
        return cached_schema

    # 缓存未命中或已过期，生成新的 Schema
    logger.info(f"生成新的 Schema: {cache_key}")
    db_engine = get_db_engine()
    db_source = create_db_source(
        db_engine, dialect,
        global_db_config.get("database", ""),
        system_prefix=global_system_prefix
    )

    # 限制最大表数为 500
    schema_str = db_source.mschema.to_mschema(max_tables=500)

    # 添加提示信息
    num_tables = len(db_source.mschema.tables)
    if num_tables > 500:
        schema_str += (
            f"\n\n---\n"
            f"注意：数据库共有 {num_tables} 个表，但仅显示前 500 个。"
            f"如需完整 Schema，请使用 Schema 过滤功能筛选相关表。"
        )

    # 存入缓存（带 TTL）
    _set_schema_to_cache(cache_key, schema_str)

    return schema_str


@mcp.resource(dialect_scheme + "://{table_name}")
def read_resource(table_name) -> str:
    """Read table contents."""
    if is_shutting_down():
        return "服务器正在关闭，暂时不接受新请求"
    try:
        db_engine = get_db_engine()
        db_source = create_db_source(db_engine, dialect, global_db_config.get("database", ""), system_prefix=global_system_prefix)

        # 验证表名是否存在（防止SQL注入）- 白名单验证
        if table_name not in db_source.mschema.tables:
            available_tables = ", ".join(list(db_source.mschema.tables.keys())[:10])
            if len(db_source.mschema.tables) > 10:
                available_tables += ", ..."
            raise ValueError(
                f"表 '{table_name}' 不存在。可用的表: {available_tables}"
            )

        # 白名单验证后，使用参数化查询构建
        # 注意：由于 SQLAlchemy 的 text() 不支持表名参数化，我们依赖白名单验证
        # 白名单验证已经确保表名是安全的
        from sqlalchemy import Table, select
        table = Table(table_name, db_source.metadata_obj, autoload_with=db_engine)
        query = select(table)

        records, columns = db_source.fetch_with_column_name(str(query))
        result = [",".join(map(str, row)) for row in records]
        return "\n".join([",".join(columns)] + result)
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"Database error: {str(e)}")


def sql_gen_and_execute(db_env: DataBaseEnv, query: str) -> dict:
    """
    Transfers the input natural language question to sql query (known as Text-to-sql) and executes it on the database.

    Args:
        db_env: Database environment
        query: natural language to query the database. e.g. 查询在2024年每个月，卡宴的各经销商销量分别是多少

    Returns:
        dict: 包含以下键之一：
            - 成功时: {"truncated_results": [...], "fields": [...]}
            - 失败时: {"error": "错误消息", "error_type": "异常类型名"}
    """

    # db_env = context_variables.get('db_env', None)
    prompt = f"""你现在是一名{db_env.dialect}数据分析专家，你的任务是根据参考的数据库schema和用户的问题，编写正确的SQL来回答用户的问题，生成的SQL用``sql 和```包围起来。
注意：
1、表名已经包含了完整的 schema 前缀（如 sundb_metrics.table_name），请直接引用这些表名，**禁止**添加 'public.' 或其他任何额外的库名/Schema 前缀。
2、只生成一个 SQL 语句。

【数据库schema】
{db_env.mschema_str}

【问题】
{query}
"""
    logger.debug(f"SQL generation prompt: {prompt}")

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"用户的问题是: {query}"},
    ]
    param = {
        "model": model_config["name"],
        "messages": messages,
        "key": model_config["key"],
        "url": model_config["url"],
        "api_version": model_config.get("api_version"),
    }

    try:
        response = call_openai_sdk(**param)
        content = response.choices[0].message.content
        logger.debug(f"LLM Raw Response: {content}")
        sql_query = extract_sql_from_qwen(content)
        logger.info(f"Extracted SQL: {sql_query}")
        
        status, res = db_env.database.fetch(sql_query)
        if not status:
            logger.warning(f"Initial SQL execution failed: {res}. Starting fix loop...")
            for idx in range(5):
                sql_query = sql_fix(
                    db_env.dialect, db_env.mschema_str, query, sql_query, res
                )
                logger.info(f"Fixed SQL (Attempt {idx+1}): {sql_query}")
                status, res = db_env.database.fetch(sql_query)
                if status:
                    logger.info("SQL fix successful.")
                    break
            if not status:
                logger.error(f"SQL fix failed after 5 attempts. Last error: {res}")

        sql_res = db_env.database.fetch_truncated(sql_query, max_rows=100)
        logger.info(f"SQL result count: {len(sql_res.get('truncated_results', []))}")
        # 返回原始字典，让调用方根据 format 参数格式化
        return sql_res

    except Exception as e:
        logger.error(f"SQL generation or execution failed: {e}", exc_info=True)
        return {
            "error": str(e),
            "error_type": type(e).__name__
        }


def sql_fix(
    dialect: str, mschema: str, query: str, sql_query: str, error_info: str
):
    system_prompt = """现在你是一个{dialect}数据分析专家，需要阅读一个客户的问题，参考的数据库schema，该问题对应的待检查SQL，以及执行该SQL时数据库返回的语法错误，请你仅针对其中的语法错误进行修复，输出修复后的SQL。
注意：
1、仅修复语法错误，不允许改变SQL的逻辑。
2、生成的SQL用```sql 和```包围起来。

【数据库schema】
{schema}
""".format(dialect=dialect, schema=mschema)
    user_prompt = """【问题】
{question}

【待检查SQL】
{sql}

【错误信息】
{sql_res}""".format(question=query, sql=sql_query, sql_res=error_info)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    param = {
        "model": model_config["name"],
        "messages": messages,
        "key": model_config["key"],
        "url": model_config["url"],
    }

    response = call_openai_sdk(**param)
    content = response.choices[0].message.content
    sql_query = extract_sql_from_qwen(content)

    return sql_query


def format_result(result: dict, format_type: str = "markdown") -> str:
    """将查询结果格式化为指定格式

    Args:
        result: 包含 truncated_results 和 fields 的字典
        format_type: 格式类型 (markdown, json, csv)
    """
    import json as json_module

    # result 应该始终是 dict 类型
    if not isinstance(result, dict):
        logger.error(f"Unexpected result type: {type(result)}, value: {result}")
        return f"Error: Unexpected result type {type(result).__name__}"

    fields = result.get("fields", [])
    rows = result.get("truncated_results", [])

    # 检查是否为错误结果（truncated_results 是字符串而非列表）
    if isinstance(rows, str):
        # 这是一个错误消息，直接返回
        return f"Error: {rows}"

    # 确保 rows 是列表格式
    if not isinstance(rows, list):
        return f"Unexpected result format: {str(result)}"

    if format_type == "json":
        # JSON 格式
        data = []
        for row in rows:
            row_dict = {}
            for i, field in enumerate(fields):
                row_dict[field] = row[i] if i < len(row) else None
            data.append(row_dict)
        return json_module.dumps({"data": data, "fields": fields}, ensure_ascii=False, indent=2)

    elif format_type == "csv":
        # CSV 格式
        if not fields:
            return "No data available"
        lines = [",".join(fields)]
        for row in rows:
            lines.append(",".join(str(v) for v in row))
        return "\n".join(lines)

    else:
        # 默认 Markdown 格式
        if not fields:
            return str(result)
        header = "| " + " | ".join(fields) + " |"
        separator = "| " + " | ".join(["---"] * len(fields)) + " |"
        data_rows = []
        for row in rows:
            data_rows.append("| " + " | ".join(str(v) for v in row) + " |")
        return "\n".join([header, separator] + data_rows)


def call_xiyan(query: str, format_type: str = "markdown") -> str:
    """Fetch the data from database through a natural language query

    Args:
        query: The query in natual language
        format_type: Output format (markdown, json, csv)
    """
    global schema_retriever

    # 检查服务器是否正在关闭
    if is_shutting_down():
        return "服务器正在关闭，暂时不接受新请求"

    logger.info(f"Calling tool with arguments: {query}")
    try:
        db_engine = get_db_engine()
        db_source = create_db_source(db_engine, dialect, global_db_config.get("database", ""), system_prefix=global_system_prefix)
    except Exception as e:
        return "数据库连接失败" + str(e)

    logger.info("Calling xiyan")

    # Schema 过滤
    if schema_filter_enabled:
        try:
            # 使用封装函数获取 Schema 检索器（线程安全）
            retriever = get_schema_retriever(db_source)

            # 检索相关表并构建 Sub-Schema
            table_names, sub_schema = retriever.retrieve_and_build(
                query,
                database=global_db_config.get("database"),
                system_prefix=global_system_prefix
            )
            logger.info(f"Schema 过滤：检索到 {len(table_names)} 个表: {table_names}")
            logger.debug(f"生成的 Sub-Schema 长度: {len(sub_schema)} 字符")
            
            # 创建使用 Sub-Schema 的环境
            env = DataBaseEnv(db_source)
            env.mschema_str = sub_schema  # 覆盖为精简 Schema
            
        except Exception as e:
            logger.warning(f"Schema 过滤失败，使用完整 Schema: {e}")
            env = DataBaseEnv(db_source)
    else:
        env = DataBaseEnv(db_source)
    
    res = sql_gen_and_execute(env, query)

    # 检查是否为错误
    if "error" in res:
        return f"错误: {res['error']}"

    # 格式化正常结果
    if "truncated_results" in res:
        return format_result(res, format_type)

    # 不应该到达这里，但作为防御性编程
    return f"未知响应格式: {str(res)}"


@mcp.tool()
def get_data(query: str, format: str = "markdown") -> list[TextContent]:
    """Fetch the data from database through a natural language query

    Args:
        query: The query in natural language
        format: Output format - 'markdown' (default), 'json', or 'csv'
    """

    res = call_xiyan(query, format_type=format)
    return [TextContent(type="text", text=res)]


def main():
    parser = argparse.ArgumentParser(description="Run MCP server.")
    parser.add_argument(
        "transport",
        nargs="?",
        default="stdio",
        choices=["stdio", "streamable-http", "sse"],
        help="Transport type (stdio, streamable-http or sse)",
    )
    parser.add_argument(
        "--host", default="localhost", help="host for the http transport"
    )

    parser.add_argument(
        "--port", type=int, default=8000, help="port for the http transport"
    )
    args = parser.parse_args()

    if args.transport == "streamable-http":
        mcp.settings.port = args.port
        mcp.settings.host = args.host
        logger.info(f"MCP server running at {args.host}/{args.port}")
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport=args.transport)


logger.info("Server 模块加载完成")

if __name__ == "__main__":
    main()
