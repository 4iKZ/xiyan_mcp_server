import argparse
import logging
import os
import signal
import sys

import yaml  # 添加yaml库导入
from mcp.server import FastMCP
from mcp.types import TextContent

from .database_env import DataBaseEnv
from .utils.db_config import DBConfig
from .utils.db_source import HITLSQLDatabase
from .utils.db_util import init_db_conn
from .utils.file_util import extract_sql_from_qwen
from .utils.llm_util import call_openai_sdk


def create_db_source(db_engine, dialect: str, db_name: str = ''):
    """根据方言创建合适的数据源"""
    if dialect.lower() in ('greptimedb', 'greptimedb_mysql'):
        from .utils.greptimedb_source import GreptimeDBSource
        return GreptimeDBSource(db_engine, db_name=db_name)
    else:
        return HITLSQLDatabase(db_engine)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("xiyan_mcp_server")


# Handle SIGINT (Ctrl+C) gracefully
def signal_handler(sig, frame):
    print("Shutting down server gracefully...")
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)


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
mcp_config = global_config.get("mcp", {})
model_config = global_config["model"]
global_db_config = global_config.get("database")
global_xiyan_db_config = get_xiyan_config(global_db_config)
dialect = global_db_config.get("dialect", "mysql")
# 规范化 dialect 作为 URL scheme（下划线不允许在 URL scheme 中）
dialect_scheme = dialect.replace("_", "-")

# 全局数据库引擎单例（避免连接池泄漏）
_db_engine = None
_db_engine_lock = None  # 延迟初始化 threading.Lock

def get_db_engine():
    """获取全局数据库引擎单例，避免每次请求创建新引擎导致连接池泄漏"""
    global _db_engine, _db_engine_lock
    if _db_engine is None:
        if _db_engine_lock is None:
            import threading
            _db_engine_lock = threading.Lock()
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

# 初始化 Schema 检索器（如果启用）
schema_retriever = None
_schema_retriever_lock = None  # 延迟初始化 threading.Lock
if schema_filter_enabled:
    try:
        import redis
        from .utils.embedding_service import EmbeddingService
        from .utils.schema_retriever import SchemaRetriever
        
        # 初始化 Redis
        redis_client = redis.Redis(
            host=redis_config.get("host", "localhost"),
            port=redis_config.get("port", 6379),
            password=redis_config.get("password") or None,
            decode_responses=False
        )
        redis_client.ping()
        logger.info("Schema 过滤已启用，Redis 连接成功")
        
        # 初始化 Embedding 服务
        embedding_service = EmbeddingService(embedding_config)
        logger.info(f"Embedding 模型已加载: {embedding_config.get('model', 'default')}")
        
        logger.debug("正在准备 Schema 检索器配置...")
        # Schema 检索器将在首次查询时初始化（需要 mschema）
        _embedding_service = embedding_service
        _redis_client = redis_client
        _retriever_config = {
            "index_name": redis_config.get("index_name", "xiyan_schema"),
            "top_k": schema_filter_config.get("top_k", 5),
            "score_threshold": schema_filter_config.get("score_threshold", 0.6)
        }
        logger.info("Schema 检索器配置完成")
        
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
async def read_resource() -> str:
    db_engine = get_db_engine()
    db_source = create_db_source(db_engine, dialect, global_db_config.get("database", ""))
    return db_source.mschema.to_mschema()


@mcp.resource(dialect_scheme + "://{table_name}")
async def read_resource(table_name) -> str:
    """Read table contents."""
    try:
        db_engine = get_db_engine()
        db_source = create_db_source(db_engine, dialect, global_db_config.get("database", ""))

        # 验证表名是否存在（防止SQL注入）
        if table_name not in db_source.mschema.tables:
            available_tables = ", ".join(list(db_source.mschema.tables.keys())[:10])
            if len(db_source.mschema.tables) > 10:
                available_tables += ", ..."
            raise ValueError(
                f"表 '{table_name}' 不存在。可用的表: {available_tables}"
            )

        # 验证表名格式（只允许字母、数字、下划线）
        import re
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name):
            raise ValueError(f"表名格式无效: '{table_name}'")

        records, columns = db_source.fetch_with_column_name(
            f"SELECT * FROM {table_name}"
        )
        result = [",".join(map(str, row)) for row in records]
        return "\n".join([",".join(columns)] + result)
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"Database error: {str(e)}")


def sql_gen_and_execute(db_env: DataBaseEnv, query: str):
    """
    Transfers the input natural language question to sql query (known as Text-to-sql) and executes it on the database.
     Args:
        query: natural language to query the database. e.g. 查询在2024年每个月，卡宴的各经销商销量分别是多少
    """

    # db_env = context_variables.get('db_env', None)
    prompt = f"""你现在是一名{db_env.dialect}数据分析专家，你的任务是根据参考的数据库schema和用户的问题，编写正确的SQL来回答用户的问题，生成的SQL用``sql 和```包围起来。
【数据库schema】
{db_env.mschema_str}

【问题】
{query}
"""
    # logger.info(f"SQL generation prompt: {prompt}")

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
        sql_query = extract_sql_from_qwen(content)
        status, res = db_env.database.fetch(sql_query)
        if not status:
            for idx in range(3):
                sql_query = sql_fix(
                    db_env.dialect, db_env.mschema_str, query, sql_query, res
                )
                status, res = db_env.database.fetch(sql_query)
                if status:
                    break

        sql_res = db_env.database.fetch_truncated(sql_query, max_rows=100)
        logger.info(f"SQL query: {sql_query}\nSQL result: {sql_res}")
        # 返回原始字典，让调用方根据 format 参数格式化
        return sql_res

    except Exception as e:
        return str(e)


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
    
    # 解析 result 字符串为字典（如果是字符串）
    if isinstance(result, str):
        # 尝试从字符串中提取数据
        return result  # 如果无法解析，直接返回原始字符串
    
    fields = result.get("fields", [])
    rows = result.get("truncated_results", [])
    
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
    global schema_retriever, _schema_retriever_lock

    logger.info(f"Calling tool with arguments: {query}")
    try:
        db_engine = get_db_engine()
        db_source = create_db_source(db_engine, dialect, global_db_config.get("database", ""))
    except Exception as e:
        return "数据库连接失败" + str(e)

    logger.info("Calling xiyan")

    # Schema 过滤
    if schema_filter_enabled:
        try:
            # 延迟初始化 Schema 检索器（线程安全）
            if schema_retriever is None:
                if _schema_retriever_lock is None:
                    import threading
                    _schema_retriever_lock = threading.Lock()
                with _schema_retriever_lock:
                    # Double-check locking
                    if schema_retriever is None:
                        from .utils.schema_retriever import SchemaRetriever
                        schema_retriever = SchemaRetriever(
                            _redis_client,
                            _embedding_service,
                            db_source.mschema,
                            _retriever_config
                        )
                        logger.info("Schema 检索器已初始化")
            
            # 检索相关表并构建 Sub-Schema
            table_names, sub_schema = schema_retriever.retrieve_and_build(
                query,
                database=global_db_config.get("database")
            )
            logger.info(f"Schema 过滤：检索到 {len(table_names)} 个表: {table_names}")
            
            # 创建使用 Sub-Schema 的环境
            env = DataBaseEnv(db_source)
            env.mschema_str = sub_schema  # 覆盖为精简 Schema
            
        except Exception as e:
            logger.warning(f"Schema 过滤失败，使用完整 Schema: {e}")
            env = DataBaseEnv(db_source)
    else:
        env = DataBaseEnv(db_source)
    
    res = sql_gen_and_execute(env, query)
    
    # 格式化结果
    if isinstance(res, dict) and "truncated_results" in res:
        return format_result(res, format_type)
    
    return str(res)


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
