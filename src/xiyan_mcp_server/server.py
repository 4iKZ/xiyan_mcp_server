import argparse
import difflib
import logging
import os
import re
import signal
import sys
import time
import threading
from datetime import datetime
from typing import Dict, Optional

import yaml  # 添加yaml库导入
from starlette.middleware.trustedhost import TrustedHostMiddleware

from mcp.server import FastMCP
from mcp.types import TextContent

from .database_env import DataBaseEnv
from .utils.db_config import DBConfig
from .utils.db_source import HITLSQLDatabase, shutdown_sql_executor
from .utils.db_util import init_db_conn
from .utils.file_util import extract_sql_from_qwen
from .utils.hdfs_util import HDFSUploader, convert_to_parquet_and_upload
from .utils.llm_util import call_openai_sdk
from .utils.query_tracker import (
    get_query_tracker, classify_error, extract_tables_from_sql,
    extract_relevant_schema, count_available_tables,
)


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

    # 清理 SQL 执行线程池
    try:
        shutdown_sql_executor()
    except Exception as e:
        logger.error(f"关闭 SQL 线程池时出错: {e}")

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


def _expand_env_vars(obj):
    """
    递归展开配置中的环境变量

    支持格式：
    - ${VAR} - 必需的环境变量
    - ${VAR:-default} - 带默认值的环境变量
    """
    import re

    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    elif isinstance(obj, str):
        # 匹配 ${VAR} 或 ${VAR:-default} 格式
        pattern = r'\$\{([^:}]+)(?::-([^}]*))?\}'

        def replace_env_var(match):
            var_name = match.group(1)
            default_value = match.group(2) if match.group(2) is not None else ""
            return os.environ.get(var_name, default_value)

        return re.sub(pattern, replace_env_var, obj)
    else:
        return obj

def get_yml_config():
    config_path = os.getenv(
        "YML", os.path.join(os.path.dirname(__file__), "config.yml")
    )
    logger.info(f"Loading configuration from {config_path}")
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
        # 展开环境变量
        config = _expand_env_vars(config)
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

# 全局 db_source 单例（避免每个请求重复创建数据源对象）
_db_source = None
_db_source_lock = threading.Lock()


def get_db_source():
    """获取全局数据源单例（线程安全）

    共享同一个 db_source 对象，底层的 SQLAlchemy Engine 连接池已共享，
    同时避免 HITLSQLDatabase/GreptimeDBSource 的重复初始化开销
    （如 MetaData reflect、表列表加载等）。
    """
    global _db_source
    if _db_source is None:
        with _db_source_lock:
            if _db_source is None:
                _db_source = create_db_source(
                    get_db_engine(), dialect,
                    global_db_config.get("database", ""),
                    system_prefix=global_system_prefix
                )
                logger.info("全局 db_source 已初始化")
    return _db_source

# Schema 过滤配置
schema_filter_config = global_config.get("schema_filter", {})
schema_filter_enabled = schema_filter_config.get("enabled", False)
table_list_path = schema_filter_config.get("table_list", None)  # 可选：全量表模式用的表名清单

# 加载 table_list（仅在 schema_filter 未启用时加载，与 config.yml 注释一致）
_FULL_TABLE_LIST = None
if table_list_path and not schema_filter_enabled:
    if not os.path.isabs(table_list_path):
        table_list_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', '..', table_list_path
        )
    try:
        with open(table_list_path) as f:
            _FULL_TABLE_LIST = [line.strip() for line in f if line.strip()]
        logger.info(f"Loaded {len(_FULL_TABLE_LIST)} tables from {table_list_path}")
    except Exception as e:
        logger.warning(f"Failed to load table list from {table_list_path}: {e}")
        _FULL_TABLE_LIST = None

def _qualify_table_name(name: str) -> str:
    """为裸表名添加 schema 前缀（cockroachdb* → cockroach_logs.，已含 '.' → 原样，其它 → cockroach_metrics.）"""
    if name.startswith("cockroachdb"):
        return f"cockroach_metrics.{name}"
    if "." in name:
        return name
    return f"cockroach_metrics.{name}"


def _build_table_only_mschema(table_names: list, db_name: str = "cockroach") -> str:
    """构建极简 M-Schema：通用列说明 + 表名列表（无字段详情）"""
    COLUMN_LEGEND = (
        "【通用列说明】cockroach_metrics 下的表为 Prometheus 指标格式，列结构高度统一：\n"
        "- 所有表都有：greptime_timestamp (TIMESTAMP, 采集时间), greptime_value (FLOAT, 指标值),\n"
        "  instance (STRING, 采集实例), job (STRING, 采集任务名)\n"
        "- 99.7% 的表有：node_id (STRING, CockroachDB 节点 ID)\n"
        "- 约 30% 的表额外有：store (STRING, 存储 ID)\n"
        "- 约 6% 的表额外有：le (FLOAT, 直方图桶上界)\n"
    )
    lines = [COLUMN_LEGEND, f"【DB_ID】 {db_name}", "【Schema】"]
    for t in sorted(table_names):
        full_name = _qualify_table_name(t)
        lines.append(f"# Table: {full_name}")
        lines.append("[")
        lines.append("]")
    return "\n".join(lines)

embedding_config = global_config.get("embedding", {})
redis_config = global_config.get("redis", {})

# HDFS 配置
hdfs_config = global_config.get("hdfs", {})
hdfs_enabled = hdfs_config.get("enabled", False)

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

# 全局 HDFS 上传器单例（线程安全）
_hdfs_uploader = None
_hdfs_uploader_lock = threading.Lock()

def get_hdfs_uploader():
    """获取全局 HDFS 上传器单例（线程安全）"""
    global _hdfs_uploader
    if _hdfs_uploader is None:
        with _hdfs_uploader_lock:
            if _hdfs_uploader is None:
                try:
                    _hdfs_uploader = HDFSUploader(hdfs_config)
                    logger.info(f"HDFS 上传器已初始化: {hdfs_config.get('remote_host')}")
                except Exception as e:
                    logger.error(f"HDFS 上传器初始化失败: {e}")
                    raise
    return _hdfs_uploader

# Schema 检索器配置（延迟初始化）
# 索引名按 database.system 自动派生：xiyan_schema + "_" + system，实现多工作空间隔离
_base_index_name = redis_config.get("index_name", "xiyan_schema")
_workspace_index_name = (
    f"{_base_index_name}_{global_system_prefix.lower()}"
    if global_system_prefix else _base_index_name
)
_stage2_config = schema_filter_config.get("stage2", {}) or {}
_retriever_config = {
    "index_name": _workspace_index_name,
    "top_k": schema_filter_config.get("top_k", 5),
    "score_threshold": schema_filter_config.get("score_threshold", 0.6),
    "stage2": _stage2_config,
    "knowledge_dir": schema_filter_config.get("knowledge_dir", "json"),
    "system_prefix": global_system_prefix,
}
logger.info(f"Schema 检索器索引名: {_workspace_index_name} (system={global_system_prefix})")
if _stage2_config.get("enabled"):
    logger.info(
        f"二级筛选已启用: model={_stage2_config.get('model_name')}, "
        f"N={_stage2_config.get('stage1_top_n')}, M={_stage2_config.get('stage2_top_m')}"
    )

# 初始化 Schema 检索器（如果启用）
schema_retriever = None
_schema_retriever_lock = threading.Lock()  # 立即初始化，避免竞态条件

# 初始化 Stage 2 筛选器（如果启用）
_stage2_filter = None
_stage2_filter_lock = threading.Lock()


def get_stage2_filter():
    """获取全局 Stage2Filter 单例（线程安全）"""
    global _stage2_filter
    if _stage2_filter is None:
        with _stage2_filter_lock:
            if _stage2_filter is None:
                from .utils.stage2_filter import Stage2Filter
                _stage2_filter = Stage2Filter(_stage2_config)
                logger.info("Stage2Filter 已初始化")
    return _stage2_filter


def get_schema_retriever(db_source):
    """获取全局 Schema 检索器单例（线程安全，避免重复初始化检查）"""
    global schema_retriever
    if schema_retriever is None:
        with _schema_retriever_lock:
            if schema_retriever is None:
                from .utils.schema_retriever import SchemaRetriever
                # 仅在 stage2 启用时构造 Stage2Filter
                stage2_filter = get_stage2_filter() if _stage2_config.get("enabled") else None
                schema_retriever = SchemaRetriever(
                    get_redis_client(),
                    get_embedding_service(),
                    db_source.mschema,
                    _retriever_config,
                    db_source=db_source,
                    stage2_filter=stage2_filter,
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
    db_source = get_db_source()

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
        db_source = get_db_source()

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


# ── 按错误类型的 retry 策略 ──
# 目标：不是减少 token 开销，而是让每次 retry 有真实的修复成功率。
# base_sql="initial"  → 每次 retry 以原始 SQL 为基线（避免链式歪传播）
# base_sql="prev"    → 增量修复，每次基于上一次产物（仅 syntax_error）
RETRY_STRATEGY = {
    "syntax_error":         {"max_retries": 3, "base_sql": "prev"},
    "type_error":           {"max_retries": 3, "base_sql": "initial"},
    "column_not_found":     {"max_retries": 2, "base_sql": "initial"},
    "table_not_found":      {"max_retries": 2, "base_sql": "initial"},
    "function_not_found":   {"max_retries": 2, "base_sql": "initial"},
    "join_error":           {"max_retries": 2, "base_sql": "initial"},
    "distinct_orderby_error":{"max_retries": 2, "base_sql": "initial"},
    "object_not_found":     {"max_retries": 2, "base_sql": "initial"},
    "planner_error":        {"max_retries": 2, "base_sql": "initial"},
    "unsupported_statement":{"max_retries": 1, "base_sql": "initial"},
    "other":                {"max_retries": 1, "base_sql": "initial"},
    # 基础设施错误：prompt 无法修复，直接跳过
    "timeout":              {"max_retries": 0, "base_sql": None},
    "permission_denied":    {"max_retries": 0, "base_sql": None},
    "connection_error":     {"max_retries": 0, "base_sql": None},
}
DEFAULT_RETRY = {"max_retries": 1, "base_sql": "initial"}


def _get_retry_strategy(error_type: str) -> dict:
    return RETRY_STRATEGY.get(error_type, DEFAULT_RETRY)


def _inject_limit(sql: str) -> tuple:
    """
    如果 SQL 最外层没有 LIMIT，自动注入一个。

    返回 (可能修改后的 SQL, 是否注入了 LIMIT)。

    - 已有外层 LIMIT → 不改
    - 含聚合函数 (COUNT/SUM/AVG/GROUP BY/stddev/var_/approx_percentile) → LIMIT 1000
    - 其它 → LIMIT 500
    """
    import re

    if not sql or not sql.strip():
        return sql, False

    # 检查最外层是否有 LIMIT（去掉括号内容后检查，避免子查询/CTE 内的 LIMIT 干扰）
    stripped = re.sub(r'\([^()]*\)', '', sql, flags=re.IGNORECASE)
    if re.search(r'\bLIMIT\s+\d+', stripped, re.IGNORECASE):
        return sql, False

    # 检测聚合函数
    has_agg = bool(re.search(
        r'\b(COUNT|SUM|AVG|MAX|MIN|GROUP\s+BY|stddev|var_|approx_percentile|WITHIN\s+GROUP)\b',
        sql, re.IGNORECASE,
    ))
    limit_val = 1000 if has_agg else 500

    # 去掉末尾分号，追加 LIMIT
    sql_stripped = sql.rstrip().rstrip(';').rstrip()
    return f"{sql_stripped} LIMIT {limit_val}", True


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
    t_start = time.time()

    # 注入当前时间，避免模型使用训练数据中的过期日期
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    time_rules = f"""4、当前实际时间是 {now_str}。当用户问题包含相对时间表达（如"最近X小时/天/周"、"今天"、"昨天"、"今天和昨天"等）时，**必须**使用动态时间函数，**禁止**硬编码具体日期：
   4.1) "最近N小时" → greptime_timestamp >= NOW() - INTERVAL 'N hours'
   4.2) "最近N天" → greptime_timestamp >= NOW() - INTERVAL 'N days'
   4.3) "今天" → greptime_timestamp >= CURRENT_DATE AND greptime_timestamp < CURRENT_DATE + INTERVAL '1 day'
   4.4) "昨天" → greptime_timestamp >= CURRENT_DATE - INTERVAL '1 day' AND greptime_timestamp < CURRENT_DATE
   4.5) "今天和昨天"对比 → **禁止使用 UNION / UNION ALL / 多个 SELECT 用分号分隔**（GreptimeDB 不支持）。改写为：在同一条 SELECT 里用 SUM(CASE WHEN ...) 并行计算两列，例如 SUM(CASE WHEN greptime_timestamp >= CURRENT_DATE THEN greptime_value END) AS today_count, SUM(CASE WHEN greptime_timestamp >= CURRENT_DATE - INTERVAL '1 day' AND greptime_timestamp < CURRENT_DATE THEN greptime_value END) AS yesterday_count；从全部结果用一条 SELECT 输出
   4.6) 只有用户明确指定了绝对日期（如"2026-06-01到2026-06-02"）时，才使用硬编码日期字符串

"""

    # GreptimeDB/DataFusion 方言限制规则，告知 XiYan 避开不兼容的 PostgreSQL 语法
    dialect_rules = ""
    if "greptimedb" in db_env.dialect.lower():
        dialect_rules = """5、GreptimeDB 底层使用 DataFusion 查询引擎，以下 PostgreSQL 语法不兼容，必须避免：
   5.1) 没有 DATE() 函数，日期过滤用字符串比较或 NOW()/CURRENT_DATE 函数：greptime_timestamp >= '2026-06-01 00:00:00' 或 greptime_timestamp >= NOW() - INTERVAL '7 days'；需要按天聚合时用 date_trunc('day', 列) 替代 DATE(列)
   5.2) SELECT DISTINCT 时，ORDER BY 的所有列必须出现在 SELECT 列表中，否则 DataFusion 拒绝规划；常见错误示例：SELECT DISTINCT a.node_id FROM t1 a JOIN t2 b ... ORDER BY a.greptime_value DESC, b.greptime_value DESC → 报错 "must appear in select list"。修正方法：要么把 ORDER BY 的列加进 SELECT，要么把 ORDER BY 改为底层聚合（GROUP BY + ORDER BY SUM(...) / MAX(...)）而非 DISTINCT 后排序
   5.3) 时间戳之间不能做乘除运算（timestamp/timestamp 或 timestamp*n 等不支持）
   5.4) 多表 JOIN 或子查询中，列引用务必带表别名（如 t.node_id），避免仅写 node_id
   5.5) 时间戳值写成完整的 ISO 字符串 '2026-06-01 00:00:00'，不要简写为 '2026-06-01'
   5.6) 子查询中避免 SELECT *（DataFusion 可能无法正确展开），尽量显式列出所需列名
   5.7) date_part 等函数只能用于真正的时间戳列，不能对聚合后的数值列调用
   5.8) 部分聚合函数不支持下列 PostgreSQL 写法，必须使用对应的 DataFusion 版本：
      - 不支持 approx_percentile() → 用 approx_percentile_cont(分位数) WITHIN GROUP (ORDER BY 列)，格式为 approx_percentile_cont(0.95) WITHIN GROUP (ORDER BY greptime_value)，每个分位数单独计算一列；也支持 median(列) 作为 P50 简写
      - 不支持 variance() → 用 var_samp() 或 var_pop()
      - 不支持 percentile_disc() → 用 approx_percentile_cont() 近似替代
   5.9) 不支持 MERGE / UPDATE / DELETE 等 DML 语句，只允许 SELECT 查询
   5.10) 禁止使用 UNION / UNION ALL / INTERSECT / EXCEPT，也禁止用分号分隔多个 SELECT 语句（GreptimeDB 不支持），多时段对比改写为 SUM(CASE WHEN 条件 THEN 值 END) 同一查询内并列两列，INTERSECT/EXCEPT 改用 JOIN 或 CASE WHEN 改写
   5.11) 增量/趋势/变化类问题：优先使用自连接差分计算变化量（DataFusion 可能不支持 LAG() 窗口函数）。典型写法：SELECT a.greptime_value - b.greptime_value AS delta FROM t a JOIN t b ON a.node_id=b.node_id AND a.greptime_timestamp = b.greptime_timestamp - INTERVAL '1 hour'；如使用 LAG(GREPTIME_VALUE) OVER (PARTITION BY 维度列 ORDER BY greptime_timestamp) 报错，改用上述自连接方式；变化率：(a.greptime_value - b.greptime_value) / NULLIF(b.greptime_value, 0)。累计值指标（表名含 _total/_count_total）需特别警惕：直接 SUM/AVG 是常见错误
   5.12) 时间粒度聚合：当用户问题包含"每小时"、"每分钟"、"按小时汇总"、"按天汇总"等时间粒度词时，SQL **必须**使用 DATE_TRUNC 显式指定时间桶："每小时" → date_trunc('hour', greptime_timestamp)；"每分钟" → date_trunc('minute', greptime_timestamp)；"按天" → date_trunc('day', greptime_timestamp)。GreptimeDB 不支持 DATE()，DATE_TRUNC 是唯一合法的时间聚合方式
   5.13) 多指标联合查询：当用户问题包含"结合"、"对比"、"同时满足"、"比值"、"两边都"等词时，SQL **必须**从多个相关表 JOIN 查询，禁止只查一张表就声称"结合了两个指标"。Cockroach 监控表常成对出现：xxx_count（采样次数）+ xxx_sum（采样总和），"平均耗时/平均延迟"必须 SUM(_sum) / SUM(_count) JOIN 两张表，仅 AVG(_count) 是常见错误
   5.14) 分组聚合：当用户问题包含"每个X"、"按X分组"、"各类别"、"各节点"、"每个实例"等词时，SQL **必须**使用 GROUP BY 维度列。仅有 SUM/AVG/MAX 而无 GROUP BY 是常见错误，会导致返回单一聚合值而非按维度拆分的多行结果

"""

    prompt = f"""你现在是一名{db_env.dialect}数据分析专家，你的任务是根据参考的数据库schema和用户的问题，编写正确的SQL来回答用户的问题，生成的SQL用```sql 和```包围起来。
注意：
1、表名已经包含了完整的 schema 前缀（如 sundb_metrics.table_name），请直接引用这些表名，**禁止**添加 'public.' 或其他任何额外的库名/Schema 前缀。表名区分大小写，必须与 schema 中显示的完全一致（如 schedules_backup 而非 schedules_BACKUP）。
2、只生成一个 SQL 语句。
3、对于查询明细数据（不含 COUNT/SUM/AVG/GROUP BY 等聚合），请在 SQL 末尾加 LIMIT 500 限制返回行数。
{time_rules}{dialect_rules}【数据库schema】
{db_env.mschema_str}
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

    # --- 追踪数据 ---
    tracking = {
        "initial_sql": "",
        "schema_used": db_env.mschema_str,
        "exec_error": None,
        "error_type": None,
        "retries": [],
        "tables_used": [],
    }

    try:
        t_llm_start = time.time()
        response = call_openai_sdk(**param)
        llm_latency_ms = (time.time() - t_llm_start) * 1000
        content = response.choices[0].message.content
        logger.debug(f"LLM Raw Response: {content}")
        sql_query = extract_sql_from_qwen(content)
        logger.info(f"Extracted SQL: {sql_query}")

        # LLM 返回空 SQL（无有效代码块）时直接返回错误，不进入数据库执行
        if not sql_query or not sql_query.strip():
            logger.warning(f"LLM 生成空 SQL，跳过执行: {content[:200]}")
            raise ValueError("LLM 生成的 SQL 为空，需重新生成")

        # ── 自动注入 LIMIT（若 SQL 最外层无 LIMIT）──
        sql_query, limit_injected = _inject_limit(sql_query)
        if limit_injected:
            logger.info(f"自动注入 LIMIT → 新 SQL: {sql_query}")
        tracking["limit_injected"] = limit_injected

        tracking["initial_sql"] = sql_query or ""
        tracking["llm_latency_ms"] = round(llm_latency_ms, 2)

        _data: list = []
        _columns: list = []
        try:
            fetch_result = db_env.database.fetch(sql_query)
        except ValueError as e:
            fetch_result = (False, str(e))
        status = fetch_result[0]
        if status:
            _data, _columns = fetch_result[1]
        else:
            res = fetch_result[1]
        if not status:
            logger.warning(f"Initial SQL execution failed: {res}. Starting fix loop...")
            tracking["exec_error"] = str(res) if res else "unknown"
            error_type = classify_error(str(res))
            tracking["error_type"] = error_type

            strategy = _get_retry_strategy(error_type)
            max_retries = strategy["max_retries"]
            base_sql = strategy["base_sql"]

            if max_retries == 0:
                logger.info(
                    f"错误类型 '{error_type}' 为基础设施错误，跳过修复循环"
                )
            else:
                prev_sql = sql_query
                initial_sql = sql_query  # 保留原始 SQL，大部分错误类型以此为基线
                for idx in range(max_retries):
                    # 选基线：syntax_error 用上次产物增量修，其余类型从原始 SQL 出发
                    fix_base_sql = (
                        prev_sql if base_sql == "prev" else initial_sql
                    )

                    repaired_sql = sql_fix(
                        db_env.dialect, db_env.mschema_str, query,
                        fix_base_sql, res, error_type=error_type,
                        dialect_rules=dialect_rules, time_rules=time_rules,
                    )
                    repaired_sql, _ = _inject_limit(repaired_sql)
                    logger.info(
                        f"Fixed SQL (Attempt {idx+1}/{max_retries}, "
                        f"type={error_type}): {repaired_sql}"
                    )
                    try:
                        fetch_result = db_env.database.fetch(repaired_sql)
                    except ValueError as e:
                        fetch_result = (False, str(e))
                    status = fetch_result[0]
                    if status:
                        _data, _columns = fetch_result[1]
                    else:
                        res = fetch_result[1]

                    retry_record = {
                        "attempt": idx + 1,
                        "error_type": error_type,
                        "failed_sql": fix_base_sql,
                        "error_msg": str(res) if not status and res else "",
                        "repaired_sql": repaired_sql,
                        "success": status,
                    }
                    tracking["retries"].append(retry_record)

                    if status:
                        logger.info("SQL fix successful.")
                        sql_query = repaired_sql
                        break
                    prev_sql = repaired_sql

                if not status:
                    logger.error(
                        f"SQL fix failed after {max_retries} attempts. "
                        f"Error type: {error_type}. Last error: {res}"
                    )
                    tracking["exec_error"] = str(res) if res else tracking["exec_error"]

        if status:
            # 内存截断代替 fetch_truncated 二次 SQL 执行
            truncated = []
            for row in _data[:50000]:
                truncated.append(tuple(
                    str(v)[:30] if v is not None else "" for v in row
                ))
            sql_res = {"truncated_results": truncated, "fields": _columns}
            logger.info(f"SQL result count: {len(truncated)}")
        else:
            sql_res = {"truncated_results": str(res), "fields": [], "error": str(res)}
            logger.warning(f"SQL 执行失败: {error_type}")

        # 提取表名
        tracking["tables_used"] = extract_tables_from_sql(sql_query)
        tracking["total_latency_ms"] = round((time.time() - t_start) * 1000, 2)

        # 返回原始字典，让调用方根据 format 参数格式化
        sql_res["_tracking"] = tracking
        return sql_res

    except ValueError as e:
        err_msg = str(e)
        logger.warning(f"SQL validation failed: {err_msg}")
        tracking["exec_error"] = err_msg
        tracking["error_type"] = classify_error(err_msg)
        tracking["total_latency_ms"] = round((time.time() - t_start) * 1000, 2)
        return {
            "error": err_msg,
            "error_type": tracking["error_type"],
            "_tracking": tracking,
        }
    except Exception as e:
        logger.error(f"SQL generation or execution failed: {e}", exc_info=True)
        tracking["exec_error"] = str(e)
        tracking["error_type"] = type(e).__name__
        tracking["total_latency_ms"] = round((time.time() - t_start) * 1000, 2)
        return {
            "error": str(e),
            "error_type": type(e).__name__,
            "_tracking": tracking,
        }


def sql_fix(
    dialect: str, mschema: str, query: str, sql_query: str, error_info: str,
    error_type: str = "other",
    dialect_rules: str = "", time_rules: str = "",
):
    """根据错误类型选择不同的修复 prompt，使每次 retry 都有真实修复概率"""

    # ── 按错误类型定制 system prompt ──
    #    prompt 里写 {dialect} 和 {schema} 两个占位符，format 后拼上完整 schema
    PROMPT_VARIANTS = {
        "syntax_error": (
            "现在你是一个{dialect}数据分析专家。下面的SQL执行时出现了**语法错误**。"
            "请修复SQL中的语法问题。\n"
            "注意：\n"
            "1. 仅修复语法错误，不允许改变SQL的逻辑。\n"
            "2. 生成的SQL用```sql 和```包围起来。\n"
        ),
        "column_not_found": (
            "现在你是一个{dialect}数据分析专家。下面的SQL执行时报告**列不存在**。"
            "请仔细检查数据库schema，尝试替换为schema中实际存在的列名。\n"
            "注意：\n"
            "1. 只修改列名，不改变SQL的查询逻辑。\n"
            "2. 如果原列名在schema中找不到，尝试语义最接近的列名。\n"
            "3. 如果确实没有匹配的列，可以适当简化查询（如用 SELECT * 替代）。\n"
            "4. 生成的SQL用```sql 和```包围起来。\n"
        ),
        "table_not_found": (
            "现在你是一个{dialect}数据分析专家。下面的SQL执行时报告**表不存在**。"
            "请仔细检查数据库schema，尝试替换为schema中实际存在的表名。\n"
            "注意：\n"
            "1. 只修改表名，不改变SQL的查询逻辑。\n"
            "2. 在schema中查找语义最接近的表名。\n"
            "3. 如果确实没有匹配的表，请在输出中说明。\n"
            "4. 生成的SQL用```sql 和```包围起来。\n"
        ),
        "function_not_found": (
            "现在你是一个{dialect}数据分析专家。下面的SQL执行时报告**函数不存在**。"
            "请尝试用数据库实际支持的函数替换。\n"
            "GreptimeDB/DataFusion 支持的聚合函数：COUNT, SUM, AVG, MIN, MAX, STDDEV, var_samp, "
            "var_pop, approx_percentile_cont(分位数) WITHIN GROUP (ORDER BY 列), percentile_cont(分位数) WITHIN GROUP (ORDER BY 列)。\n"
            "注意：\n"
            "1. approx_percentile → 必须写成 approx_percentile_cont(0.95) WITHIN GROUP (ORDER BY col)，不是 approx_percentile(col, 0.95)\n"
            "2. variance → 用 var_samp 或 var_pop 替代\n"
            "3. 如果无法找到等价函数，可以简化查询逻辑（如用 ORDER BY + LIMIT 近似分位数）\n"
            "4. LAG()/LEAD() 等窗口函数可能不被 DataFusion 支持，改用自连接差分计算。\n"
            "5. 生成的SQL用```sql 和```包围起来。\n"
        ),
        "type_error": (
            "现在你是一个{dialect}数据分析专家。下面的SQL执行时出现了**类型/强制转换错误**。"
            "请添加或修正CAST类型转换。\n"
            "注意：\n"
            "1. 添加适当的 CAST(expr AS type) 转换不兼容的类型。\n"
            "2. 对于比较操作，确保两边类型一致。\n"
            "3. 对于聚合函数参数，确保类型正确。\n"
            "4. 生成的SQL用```sql 和```包围起来。\n"
        ),
        "join_error": (
            "现在你是一个{dialect}数据分析专家。下面的SQL执行时出现了**JOIN/歧义引用错误**。"
            "请给所有有歧义的列引用加上表名前缀。\n"
            "注意：\n"
            "1. 列名需要加上完整的表名前缀（如 table.column）。\n"
            "2. 如果使用了别名，请使用别名前缀。\n"
            "3. 如果使用了 SELECT DISTINCT + ORDER BY，确保 ORDER BY 的列都在 SELECT 列表中，或改用 GROUP BY + 聚合函数替代 DISTINCT。\n"
            "4. 生成的SQL用```sql 和```包围起来。\n"
        ),
        "unsupported_statement": (
            "现在你是一个{dialect}数据分析专家。下面的SQL执行时报告**语句不被支持**。"
            "请检查SQL开头是否有多余的关键词（如 'sql' 前缀），并清理SQL。\n"
            "注意：\n"
            "1. 确保SQL以 SELECT 开头，没有多余的前缀词。\n"
            "2. 确保只有一条SQL语句。\n"
            "3. 移除任何非标准的语法结构。\n"
            "4. 生成的SQL用```sql 和```包围起来。\n"
        ),
        "planner_error": (
            "现在你是一个{dialect}数据分析专家。下面的SQL执行时数据库规划器返回了错误。"
            "请尝试简化SQL，可能涉及子查询重写、使用更简单的表达式。\n"
            "注意：\n"
            "1. 可以拆分子查询为更简单的形式。\n"
            "2. 简化 WHERE 子句中的复杂表达式。\n"
            "3. 如果使用了窗口函数或CTE，尝试重写为简单查询。\n"
            "4. 生成的SQL用```sql 和```包围起来。\n"
        ),
        "distinct_orderby_error": (
            "现在你是一个{dialect}数据分析专家。下面的SQL因 SELECT DISTINCT + ORDER BY 列不在 SELECT 列表中被拒绝。\n"
            "修复方法（优先用方法1）：\n"
            "1. 改用 GROUP BY + 聚合函数（MAX/SUM）替代 DISTINCT 后排序，如：\n"
            "   SELECT node_id, MAX(greptime_value) AS max_val FROM t GROUP BY node_id ORDER BY max_val DESC\n"
            "2. 或将 ORDER BY 的列加入 SELECT 列表。\n"
            "注意：\n"
            "1. 生成的SQL用```sql 和```包围起来。\n"
        ),
    }

    # 通用 prompt（other / object_not_found / unknown 等）
    DEFAULT_PROMPT = (
        "现在你是一个{dialect}数据分析专家。下面的SQL执行时数据库返回了错误。"
        "请根据错误信息修复SQL。\n"
        "注意：\n"
        "1. 只修改必要的部分，尽量保持原SQL的逻辑不变。\n"
        "2. 如果错误无法修复，可以尝试生成等价但语法不同的SQL。\n"
        "3. 生成的SQL用```sql 和```包围起来。\n"
    )

    prompt_template = PROMPT_VARIANTS.get(error_type, DEFAULT_PROMPT)
    system_prompt = prompt_template.format(dialect=dialect)
    if dialect_rules:
        system_prompt += f"\n{dialect_rules}"
    if time_rules:
        system_prompt += f"\n{time_rules}"
    system_prompt += f"\n【数据库schema】\n{mschema}"

    # ── 按错误类型注入额外上下文，提高修复成功率 ──

    # table_not_found: 注入候选表名列表（模糊匹配，带 schema 前缀）
    if error_type == "table_not_found" and _FULL_TABLE_LIST:
        wrong_tables = re.findall(r'table "([^"]+)"', error_info)
        if not wrong_tables:
            wrong_tables = extract_tables_from_sql(sql_query)
        table_map = {t.lower(): t for t in _FULL_TABLE_LIST}
        candidates = []
        for wt in wrong_tables:
            short = wt.split('.')[-1] if '.' in wt else wt
            matches = difflib.get_close_matches(
                short.lower(),
                list(table_map.keys()),
                n=5, cutoff=0.3,
            )
            for m in matches:
                candidates.append(_qualify_table_name(table_map[m]))
        if candidates:
            # 去重（保持顺序）
            candidates = list(dict.fromkeys(candidates))
            system_prompt += "\n【schema 中实际存在的相似表名（请从中选择）】\n"
            system_prompt += "\n".join(f"  - {c}" for c in candidates[:10])

    # column_not_found: 注入 DataFusion 返回的 valid fields 列表
    if error_type == "column_not_found":
        valid_match = re.search(r'[Vv]alid fields are (.+?)(?:\.|\[SQL)', error_info)
        if valid_match:
            system_prompt += f"\n【该表实际存在的字段】\n{valid_match.group(1)}\n"
            system_prompt += "请只使用上述字段。\n"

    user_prompt = (
        "【问题】\n"
        "{question}\n\n"
        "【待检查SQL】\n"
        "{sql}\n\n"
        "【错误信息】\n"
        "{sql_res}"
    ).format(question=query, sql=sql_query, sql_res=error_info)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    param = {
        "model": model_config["name"],
        "messages": messages,
        "key": model_config["key"],
        "url": model_config["url"],
        "api_version": model_config.get("api_version"),
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


def call_xiyan(
    query: str,
    format_type: str = "markdown",
    stage2_enabled: Optional[bool] = None,
    stage1_n: Optional[int] = None,
    stage2_m: Optional[int] = None,
    run_tag: Optional[str] = None,
) -> str:
    """Fetch the data from database through a natural language query

    Args:
        query: The query in natual language
        format_type: Output format (markdown, json, csv)
        stage2_enabled: 临时覆盖 schema_filter.stage2.enabled（None 走配置默认值）
        stage1_n: 临时覆盖 stage2.stage1_top_n
        stage2_m: 临时覆盖 stage2.stage2_top_m
        run_tag: 实验运行标签；当 batch test 不同 (N,M) 配比时区分用，
                 传入后该次记录会落到 query_tracker_{date}_{tag}.jsonl
    """
    global schema_retriever

    # 检查服务器是否正在关闭
    if is_shutting_down():
        return "服务器正在关闭，暂时不接受新请求"

    logger.info(f"Calling tool with arguments: {query}")
    try:
        db_source = get_db_source()
    except Exception as e:
        return "数据库连接失败" + str(e)

    logger.info("Calling xiyan")

    # Schema 过滤
    filtered_table_names = []  # 记录传给 LLM 的候选表名
    retrieval_meta: Dict = {}  # 记录二级筛选元数据
    if schema_filter_enabled:
        try:
            # 使用封装函数获取 Schema 检索器（线程安全）
            retriever = get_schema_retriever(db_source)

            # 检索相关表并构建 Sub-Schema
            table_names, sub_schema, retrieval_meta = retriever.retrieve_and_build(
                query,
                database=global_db_config.get("database"),
                system_prefix=global_system_prefix,
                stage2_enabled=stage2_enabled,
                stage1_top_n=stage1_n,
                stage2_top_m=stage2_m,
            )
            filtered_table_names = table_names[:]  # 复制一份
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
        if _FULL_TABLE_LIST:
            env.mschema_str = _build_table_only_mschema(_FULL_TABLE_LIST)
            logger.info(
                f"全量表模式: 使用 {len(_FULL_TABLE_LIST)} 张表名构建 M-Schema, "
                f"{len(env.mschema_str)} 字符"
            )

    res = sql_gen_and_execute(env, query)

    # --- 记录追踪数据 ---
    tracking = res.pop("_tracking", {})
    if tracking:
        try:
            tracker = get_query_tracker()
            retries_data = tracking.get("retries", [])
            # 根据修复历史判断最终成功/失败
            if retries_data:
                exec_success = retries_data[-1].get("success", False)
            else:
                exec_success = tracking.get("exec_error") is None
            # result_preview 只取真正的列表数据
            rows = res.get("truncated_results", [])
            if isinstance(rows, list):
                result_rows = len(rows)
                result_preview = rows[:3] if rows else None
            else:
                result_rows = 0
                result_preview = None
            # Schema 过滤的表名列表（传给 LLM 的候选表）
            filtered_tables = filtered_table_names
            tables_used = tracking.get("tables_used", [])
            full_schema = tracking.get("schema_used", "")
            tracker.record_query(
                nl_query=query,
                tool="get_data",
                database=global_db_config.get("database", ""),
                dialect=dialect,
                format_type=format_type,
                schema_filtered=schema_filter_enabled,
                initial_sql=tracking.get("initial_sql", ""),
                exec_success=exec_success,
                exec_error=tracking.get("exec_error"),
                error_type=tracking.get("error_type"),
                result_rows=result_rows,
                result_preview=result_preview,
                retry_count=len(retries_data),
                retries=retries_data,
                total_latency_ms=tracking.get("total_latency_ms", 0),
                tables_used=tables_used,
                fields=res.get("fields", []),
                available_table_count=count_available_tables(full_schema),
                relevant_schema=extract_relevant_schema(full_schema, tables_used),
                filtered_table_names=filtered_tables,
                # ── 二级筛选追踪 ──
                stage2_enabled=retrieval_meta.get("stage2_enabled"),
                stage1_top_n=retrieval_meta.get("stage1_top_n"),
                stage2_top_m=retrieval_meta.get("stage2_top_m"),
                stage1_tables=retrieval_meta.get("stage1_tables"),
                stage2_tables=retrieval_meta.get("stage2_tables"),
                stage2_model=retrieval_meta.get("stage2_model"),
                run_tag=run_tag,
                limit_injected=tracking.get("limit_injected"),
            )
        except Exception as e:
            logger.error(f"追踪记录写入失败: {e}", exc_info=True)

    # 检查是否为错误
    if "error" in res:
        return f"错误: {res['error']}"

    # 格式化正常结果
    if "truncated_results" in res:
        return format_result(res, format_type)

    # 不应该到达这里，但作为防御性编程
    return f"未知响应格式: {str(res)}"


@mcp.tool()
def get_data(
    query: str,
    format: str = "markdown",
    stage2_enabled: Optional[bool] = None,
    stage1_n: Optional[int] = None,
    stage2_m: Optional[int] = None,
    run_tag: Optional[str] = None,
) -> list[TextContent]:
    """Fetch the data from database through a natural language query

    Args:
        query: The query in natural language
        format: Output format - 'markdown' (default), 'json', or 'csv'
        stage2_enabled: 临时覆盖配置中的二级筛选开关（None 表示用配置默认值）。
            实验中可逐条切换，例如 True 启用、False 关闭。
        stage1_n: 临时覆盖向量粗召回数量（仅在二级筛选启用时生效）
        stage2_m: 临时覆盖模型精筛保留数量（仅在二级筛选启用时生效）
        run_tag: 实验运行标签（如 "n20m5_flash"），同一天跑不同 (N,M) 配比时区分用；
            传入后追踪文件会变成 query_tracker_{date}_{tag}.jsonl，且每条记录会带 run_tag 字段。
    """

    res = call_xiyan(
        query,
        format_type=format,
        stage2_enabled=stage2_enabled,
        stage1_n=stage1_n,
        stage2_m=stage2_m,
        run_tag=run_tag,
    )
    return [TextContent(type="text", text=res)]


def extract_hdfs_path_from_query(query: str) -> str:
    """从自然语言查询中提取 HDFS 存储路径

    支持中文自然语言描述，LLM 会自动识别以下意图：
    - 无路径描述 → 返回空字符串（使用默认时间戳路径）
    - 仅文件名 → 如 "命名为my_report" → 返回 "my_report"
    - 仅目录 → 如 "上传到test_batch目录" → 返回 "test_batch/"
    - 完整路径 → 如 "保存到project/daily_report" → 返回 "project/daily_report"

    Args:
        query: 用户自然语言查询

    Returns:
        提取的 HDFS 路径字符串，如果未检测到路径意图则返回空字符串
    """
    import re as _re

    prompt = (
        "从用户查询中提取HDFS存储路径信息。\n"
        "\n"
        "输出格式（严格遵守，只输出路径本身，不要任何额外文字）：\n"
        "- 未指定路径 → 输出: NONE\n"
        "- 仅指定文件名 → 输出文件名（无/）\n"
        "- 仅指定目录 → 输出目录名加/结尾\n"
        "- 指定完整路径 → 输出目录/文件名\n"
        "\n"
        "示例：\n"
        "查询: \"查询数据并上传到HDFS\" → NONE\n"
        "查询: \"查询数据，命名为my_report上传\" → my_report\n"
        "查询: \"查询数据，上传到backup目录\" → backup/\n"
        "查询: \"查询数据，保存到proj/daily_report\" → proj/daily_report\n"
        "\n"
        "用户查询: " + query
    )

    def _parse_response(raw: str) -> str:
        """从 LLM 原始响应中提取路径"""
        if not raw:
            return ""
        # 拆分为行，从最后一行开始找有效内容
        lines = [ln.strip() for ln in raw.strip().split("\n")]
        for line in reversed(lines):
            if not line:
                continue
            # 去掉引号包裹
            line = line.strip("\"'`「」『』")
            # 去掉 markdown 代码块标记
            line = _re.sub(r'^```\w*', '', line)
            line = _re.sub(r'```$', '', line)
            # 去掉可能的前缀（如 "输出:"、"路径:"、"path:" 等）
            line = _re.sub(r'^(输出|路径|path|result)[：:]\s*', '', line, flags=_re.IGNORECASE)
            line = line.strip()
            if not line:
                continue
            if line.upper() in ("NONE", "NULL", "无", "默认", "空", "无路径"):
                return ""
            # 找到有效内容
            return line
        return ""

    try:
        messages = [
            {"role": "user", "content": prompt},
        ]
        param = {
            "model": model_config["name"],
            "messages": messages,
            "key": model_config["key"],
            "url": model_config["url"],
        }
        response = call_openai_sdk(**param)
        raw = response.choices[0].message.content
        logger.info(f"HDFS路径提取 LLM 原始响应: {repr(raw)}")

        content = _parse_response(raw)
        logger.info(f"HDFS路径提取 解析结果: {repr(content)}")

        if not content:
            return ""

        # 路径安全过滤
        content = HDFSUploader._sanitize_path(content)
        return content
    except Exception as e:
        logger.warning(f"提取 HDFS 路径失败（将使用默认路径）: {e}")
        return ""


def _strip_path_clauses(query: str) -> str:
    """从查询中剥离路径/存储相关的描述，保留纯数据查询部分用于 SQL 生成

    避免"存储到xxx路径下"、"命名为xxx上传"等描述被 LLM 误解析为表名或查询条件。
    """
    import re as _re

    path_keywords = [
        '上传', 'HDFS', 'hdfs', '导出', '保存', '存储', '命名', '文件名',
        '目录', '文件夹', '路径', '存放', '写入', '写出', '放在',
    ]
    data_keywords = ['查询', '查', '表格', '表', '数据', '前', '记录', '帮我']

    clauses = _re.split(r'[，,；;]', query)
    kept = []
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        is_path = any(kw in clause for kw in path_keywords)
        has_data = any(kw in clause for kw in data_keywords)
        if is_path and not has_data:
            continue
        kept.append(clause)

    result = '，'.join(kept)
    return result if result else query


@mcp.tool()
def query_and_upload_to_hdfs(query: str, session_id: str = "", hdfs_path: str = "", run_tag: Optional[str] = None) -> list[TextContent]:
    """Execute natural language query and upload results to HDFS in parquet format

    This tool performs the following steps:
    1. Translates natural language to SQL using LLM
    2. Executes the SQL query on the database
    3. Converts results to Parquet format
    4. Uploads the Parquet file to HDFS
    5. Returns the HDFS file path

    Args:
        query: The query in natural language. Can include HDFS path descriptions like:
            - "查询xxx的前10条数据，上传到HDFS" (default path)
            - "查询xxx的前10条数据，命名为my_report上传到HDFS" (custom filename)
            - "查询xxx的前10条数据，上传到HDFS的test_batch目录下" (custom dir)
            - "查询xxx的前10条数据，保存到project/report" (full path)
        session_id: Optional session ID for file naming (ignored if hdfs_path is provided)
        hdfs_path: Custom HDFS storage path. Supports:
            - Empty (default): auto-generate timestamp-based directory and filename
            - "filename": use as filename under timestamp directory
              e.g. "my_report" -> {root}/{ts}_batch_001/parquet/my_report.parquet
            - "dir/": trailing slash means directory, filename auto-generated
              e.g. "project_a/" -> {root}/project_a/{ts}.parquet
            - "dir/filename": full path with filename
              e.g. "project/report" -> {root}/project/report.parquet

    Returns:
        HDFS path where the parquet file is stored (e.g., "/user_custom_data/20250415_143022_batch_001/parquet/result.parquet")

    Raises:
        RuntimeError: If HDFS is not enabled or upload fails
    """
    if not hdfs_enabled:
        return [TextContent(
            type="text",
            text="错误: HDFS 功能未启用，请在配置文件中设置 hdfs.enabled = true"
        )]

    if is_shutting_down():
        return [TextContent(type="text", text="服务器正在关闭，暂时不接受新请求")]

    logger.info(f"HDFS Upload Query: {query}, hdfs_path: {hdfs_path}")

    # 如果没有显式提供 hdfs_path，尝试从自然语言查询中提取
    sql_query = query  # 默认用于 SQL 生成
    if not hdfs_path:
        extracted_path = extract_hdfs_path_from_query(query)
        if extracted_path:
            hdfs_path = extracted_path
            logger.info(f"从自然语言查询中提取到 HDFS 路径: {hdfs_path}")
            # 从 query 中剥离路径描述，避免干扰 SQL 生成
            sql_query = _strip_path_clauses(query)
            logger.info(f"剥离路径后的查询: {sql_query[:120]}")

    try:
        # 获取数据库连接
        db_source = get_db_source()
    except Exception as e:
        logger.error(f"数据库连接失败: {e}")
        return [TextContent(type="text", text=f"数据库连接失败: {str(e)}")]

    try:
        # 创建数据库环境
        env = DataBaseEnv(db_source)

        # Schema 过滤（如果启用）
        filtered_table_names = []
        retrieval_meta: Dict = {}
        if schema_filter_enabled:
            try:
                retriever = get_schema_retriever(db_source)
                table_names, sub_schema, retrieval_meta = retriever.retrieve_and_build(
                    query,
                    database=global_db_config.get("database"),
                    system_prefix=global_system_prefix
                )
                filtered_table_names = table_names[:]
                logger.info(f"Schema 过滤：检索到 {len(table_names)} 个表: {table_names}")
                env.mschema_str = sub_schema
            except Exception as e:
                logger.warning(f"Schema 过滤失败，使用完整 Schema: {e}")

        # 执行 SQL 查询（使用剥离路径后的查询文本）
        res = sql_gen_and_execute(env, sql_query)

        # --- 记录追踪数据 ---
        tracking = res.pop("_tracking", {})
        if tracking:
            try:
                tracker = get_query_tracker()
                retries_data = tracking.get("retries", [])
                if retries_data:
                    exec_success = retries_data[-1].get("success", False)
                else:
                    exec_success = tracking.get("exec_error") is None
                rows = res.get("truncated_results", [])
                result_rows = len(rows) if isinstance(rows, list) else 0
                full_schema = tracking.get("schema_used", "")
                tables_used = tracking.get("tables_used", [])
                retries_data = tracking.get("retries", [])
                tracker.record_query(
                    nl_query=query,
                    tool="query_and_upload_to_hdfs",
                    database=global_db_config.get("database", ""),
                    dialect=dialect,
                    hdfs_path=hdfs_path,
                    schema_filtered=schema_filter_enabled,
                    initial_sql=tracking.get("initial_sql", ""),
                    exec_success=exec_success,
                    exec_error=tracking.get("exec_error"),
                    error_type=tracking.get("error_type"),
                    result_rows=result_rows,
                    result_preview=None,
                    retry_count=len(retries_data),
                    retries=retries_data,
                    total_latency_ms=tracking.get("total_latency_ms", 0),
                    tables_used=tables_used,
                    fields=res.get("fields", []),
                    available_table_count=count_available_tables(full_schema),
                    relevant_schema=extract_relevant_schema(full_schema, tables_used),
                    filtered_table_names=filtered_table_names,
                    # ── 二级筛选追踪 ──
                    stage2_enabled=retrieval_meta.get("stage2_enabled"),
                    stage1_top_n=retrieval_meta.get("stage1_top_n"),
                    stage2_top_m=retrieval_meta.get("stage2_top_m"),
                    stage1_tables=retrieval_meta.get("stage1_tables"),
                    stage2_tables=retrieval_meta.get("stage2_tables"),
                    stage2_model=retrieval_meta.get("stage2_model"),
                    run_tag=run_tag,
                    limit_injected=tracking.get("limit_injected"),
                )
            except Exception as e:
                logger.error(f"追踪记录写入失败(hdfs): {e}", exc_info=True)

        # 检查是否为错误
        if "error" in res:
            logger.error(f"SQL 执行失败: {res['error']}")
            return [TextContent(type="text", text=f"SQL 执行失败: {res['error']}")]

        # 转换为 Parquet 并上传到 HDFS
        uploader = get_hdfs_uploader()
        hdfs_result_path = convert_to_parquet_and_upload(
            res,
            uploader,
            session_id=session_id if session_id else None,
            hdfs_path=hdfs_path if hdfs_path else None
        )

        logger.info(f"数据已上传到 HDFS: {hdfs_result_path}")

        return [TextContent(
            type="text",
            text=f"数据已成功上传到 HDFS: {hdfs_result_path}"
        )]

    except ValueError as e:
        logger.error(f"HDFS 路径验证失败: {e}")
        return [TextContent(type="text", text=f"HDFS 路径错误: {str(e)}")]
    except Exception as e:
        logger.error(f"HDFS 上传失败: {e}", exc_info=True)
        return [TextContent(type="text", text=f"HDFS 上传失败: {str(e)}")]


def main():
    import uvicorn

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
        # 禁用 DNS rebinding protection，否则 MCP SDK 的 TransportSecurityMiddleware
        # 会拒绝来自 Docker 网络的请求（如 host.docker.internal）
        mcp.settings.transport_security = None
        logger.info(f"MCP server running at {args.host}/{args.port}")

        # 获取原始 Starlette app 并添加 TrustedHostMiddleware
        # 解决 Docker 网络环境下的 "Invalid Host header" 问题
        app = mcp.streamable_http_app()

        # 添加 TrustedHostMiddleware 允许所有 Host 头
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

        config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        import anyio
        anyio.run(server.serve)

    elif args.transport == "sse":
        mcp.settings.port = args.port
        mcp.settings.host = args.host
        logger.info(f"MCP server running at {args.host}/{args.port}")
        # SSE 使用 mcp.run() 的内置支持
        mcp.run(transport="sse")
    else:
        mcp.run(transport=args.transport)


logger.info("Server 模块加载完成")

if __name__ == "__main__":
    main()
