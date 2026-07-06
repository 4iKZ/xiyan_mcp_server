"""
GreptimeDB 专用数据源类

由于 llama_index.SQLDatabase 在初始化时会使用 SQLAlchemy 自动加载表结构，
这与 GreptimeDB 的 pg_catalog 不兼容，因此需要自定义实现。
"""
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import text
from sqlalchemy.engine import Engine

from .db_mschema import MSchema
from .db_util import examples_to_str, preprocess_sql_query
from .db_source import validate_sql_query, _run_query_with_timeout

logger = logging.getLogger(__name__)

SQL_EXECUTE_TIMEOUT = int(os.getenv("SQL_EXECUTE_TIMEOUT", "60"))


class GreptimeDBSource:
    """
    GreptimeDB 专用数据源，不使用 SQLAlchemy 的自动加载功能
    """
    
    def __init__(self, engine: Engine, db_name: str = '', system_prefix: str = ''):
        logger.info(f"GreptimeDBSource.__init__ 开始: db_name={db_name}, system_prefix={system_prefix}")
        self._engine = engine
        self._db_name = db_name or self._get_db_name_from_url()
        self._system_prefix = system_prefix
        self._dialect = engine.dialect.name
        logger.info(f"GreptimeDBSource: dialect={self._dialect}, db_name={self._db_name}")
        # 使用 system_prefix 作为 db_id，且不设置 schema 前缀，因为表名已经包含了完整的 schema 名
        self._mschema = MSchema(db_id=self._system_prefix or self._db_name, schema=None)
        self._usable_tables = []
        # 表列加载锁：使用 OrderedDict 实现 LRU 缓存，最多保留 1000 个表的锁
        self._loading_locks = OrderedDict()  # {full_table_name: Lock}
        self._loading_locks_lock = threading.Lock()
        self._loading_locks_max_size = 1000  # 可配置

        # --- 尝试从缓存文件加载（跳过 init_mschema 的数据库查询） ---
        cache_path = self._get_schema_cache_path()
        if cache_path and Path(cache_path).exists():
            try:
                self._mschema.load(cache_path)
                self._usable_tables = list(self._mschema.tables.keys())
                loaded_count = sum(
                    1 for t in self._mschema.tables.values()
                    if t.get('fields')
                )
                logger.info(
                    f"Schema 缓存已加载: {cache_path}, "
                    f"{len(self._usable_tables)} 张表, {loaded_count} 张已填充列信息"
                )
                return  # 跳过 init_mschema()
            except Exception as e:
                logger.warning(f"Schema 缓存加载失败，回退到 init_mschema: {e}")

        # 回退：原有逻辑（只加载表名，不加载列信息）
        logger.info(f"GreptimeDBSource: 开始调用 init_mschema")
        self.init_mschema()
        logger.info(f"GreptimeDBSource.__init__ 完成")

    def _get_schema_cache_path(self) -> Optional[str]:
        """根据 system_prefix 派生缓存文件路径

        路径规则：json/{system_prefix}/schema_cache.json
        与知识库目录结构一致，返回 None 表示无 system_prefix（不启用缓存）。
        """
        if not self._system_prefix:
            return None
        return str(Path("json") / self._system_prefix.lower() / "schema_cache.json")

    def _get_db_name_from_url(self):
        """从引擎 URL 获取数据库名"""
        return self._engine.url.database or "public"
    
    @property
    def mschema(self) -> MSchema:
        return self._mschema
    
    @property
    def dialect(self) -> str:
        """兼容 DataBaseEnv 的 dialect 属性"""
        return self._dialect
    
    @property
    def db_name(self) -> str:
        """兼容 DataBaseEnv 的 db_name 属性"""
        return self._db_name
    
    @property
    def database(self):
        """兼容 DataBaseEnv 的接口"""
        return self
    
    def init_mschema(self):
        """初始化 MSchema，使用 INFORMATION_SCHEMA 获取元数据

        注意：由于数据库中可能有大量表（2343+），我们只获取表列表，
        不在初始化时获取列信息和示例值。这些信息会在需要时通过 Schema 过滤延迟加载。
        """
        logger.info(f"init_mschema: 开始获取表列表")
        # 获取所有匹配前缀的库和表列表
        tables_with_schema = self._get_table_names_with_schema()
        logger.info(f"init_mschema: 找到 {len(tables_with_schema)} 个表")
        self._usable_tables = [f"{s}.{t}" for s, t in tables_with_schema]

        # 只添加表名，不获取列信息（延迟加载）
        for schema_name, table_name in tables_with_schema:
            full_table_name = f"{schema_name}.{table_name}"
            # 添加空表，字段信息将在需要时通过 Schema 过滤获取
            self._mschema.add_table(full_table_name, fields={}, comment='')

        logger.info(f"init_mschema: 完成，已添加 {len(tables_with_schema)} 个表（不含列信息）")

    def _get_table_loading_lock(self, full_table_name: str) -> threading.Lock:
        """获取表的加载锁，使用 LRU 缓存限制大小"""
        # 快速路径：已存在则更新 LRU
        if full_table_name in self._loading_locks:
            with self._loading_locks_lock:
                if full_table_name in self._loading_locks:
                    # 移到末尾（标记为最近使用）
                    lock = self._loading_locks.pop(full_table_name)
                    self._loading_locks[full_table_name] = lock
                    return lock

        # 慢速路径：创建新锁
        with self._loading_locks_lock:
            # 双重检查
            if full_table_name in self._loading_locks:
                lock = self._loading_locks.pop(full_table_name)
                self._loading_locks[full_table_name] = lock
                return lock

            # 创建新锁
            lock = threading.Lock()
            self._loading_locks[full_table_name] = lock

            # LRU 淘汰：如果超过最大大小，移除最旧的锁
            if len(self._loading_locks) > self._loading_locks_max_size:
                oldest_table = next(iter(self._loading_locks))
                del self._loading_locks[oldest_table]
                logger.debug(f"LRU 淘汰表锁: {oldest_table}")

            return lock

    def _load_table_columns(self, schema_name: str, table_name: str):
        """延迟加载单个表的列信息（线程安全，使用 LRU 缓存）"""
        full_table_name = f"{schema_name}.{table_name}"

        # 快速路径：已加载则直接返回
        if self._mschema.tables.get(full_table_name, {}).get('fields'):
            return

        # 获取或创建此表的加载锁（使用 LRU 缓存方法）
        table_lock = self._get_table_loading_lock(full_table_name)

        # 加载路径：获取锁后再次检查（双重检查锁定）
        with table_lock:
            # 双重检查：可能在等待锁时已被其他线程加载
            if self._mschema.tables.get(full_table_name, {}).get('fields'):
                return

            logger.info(f"延迟加载表列信息: {full_table_name}")

            # 获取列信息
            columns = self._get_columns(schema_name, table_name)
            for col in columns:
                # 获取示例值
                try:
                    examples = self._fetch_distinct_values(schema_name, table_name, col['name'], 5)
                except Exception as e:
                    logger.debug(
                        f"获取列示例值失败: {schema_name}.{table_name}.{col['name']}: {e}"
                    )
                    examples = []
                examples = examples_to_str(examples)

                self._mschema.add_field(
                    full_table_name,
                    col['name'],
                    field_type=col['type'],
                    primary_key=False,  # GreptimeDB 不提供主键信息
                    nullable=col.get('nullable', True),
                    default=col.get('default'),
                    autoincrement=False,
                    comment='',
                    examples=examples
                )

            logger.info(f"延迟加载完成: {full_table_name}")

    def _get_table_names_with_schema(self) -> List[Tuple[str, str]]:
        """获取所有匹配前缀的 schema 和表名列表"""
        logger.info(f"_get_table_names_with_schema: system_prefix={self._system_prefix}")
        if self._system_prefix:
            query = text("""
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema LIKE :prefix
                AND table_type = 'BASE TABLE'
            """)
            params = {"prefix": f"{self._system_prefix}%"}
        else:
            query = text("""
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema = :schema
                AND table_type = 'BASE TABLE'
            """)
            params = {"schema": self._db_name}

        logger.info(f"_get_table_names_with_schema: 开始执行查询")
        with self._engine.connect() as conn:
            logger.info(f"_get_table_names_with_schema: 连接成功，执行查询")
            result = conn.execute(query, params)
            logger.info(f"_get_table_names_with_schema: 查询成功，获取结果")
            tables = [(row[0], row[1]) for row in result]
            logger.info(f"_get_table_names_with_schema: 返回 {len(tables)} 个表")
            return tables

    def _get_table_names(self) -> List[str]:
        """兼容旧方法，获取表列表"""
        tables = self._get_table_names_with_schema()
        return [f"{s}.{t}" for s, t in tables]
    
    def _get_columns(self, schema_name: str, table_name: str) -> List[Dict]:
        """获取列信息"""
        query = text("""
            SELECT 
                column_name,
                data_type,
                is_nullable,
                column_default
            FROM information_schema.columns 
            WHERE table_schema = :schema
            AND table_name = :table_name
            ORDER BY ordinal_position
        """)
        with self._engine.connect() as conn:
            result = conn.execute(query, {"schema": schema_name, "table_name": table_name})
            columns = []
            for row in result:
                col_name, data_type, is_nullable, default = row
                columns.append({
                    "name": col_name,
                    "type": self._map_type(data_type),
                    "nullable": is_nullable == "YES",
                    "default": default,
                })
            return columns
    
    def _map_type(self, data_type: str) -> str:
        """映射 GreptimeDB 类型到标准类型"""
        type_upper = data_type.upper()
        type_mapping = {
            "STRING": "VARCHAR",
            "INT8": "SMALLINT",
            "INT16": "SMALLINT",
            "INT32": "INTEGER",
            "INT64": "BIGINT",
            "UINT8": "SMALLINT",
            "UINT16": "INTEGER",
            "UINT32": "BIGINT",
            "UINT64": "BIGINT",
            "FLOAT32": "REAL",
            "FLOAT64": "DOUBLE PRECISION",
            "BOOLEAN": "BOOLEAN",
            "BINARY": "BYTEA",
            "DATE": "DATE",
            "DATETIME": "TIMESTAMP",
            "TIMESTAMP": "TIMESTAMP",
            "TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE",
        }
        return type_mapping.get(type_upper, type_upper)
    
    def _fetch_distinct_values(self, schema_name: str, table_name: str, column_name: str, max_num: int = 5) -> List:
        """获取列的不同值示例"""
        # 验证表名和列名格式（防止SQL注入）
        # 放宽限制：允许字母、数字、下划线、点号、连字符、空格、冒号
        # 禁止危险字符：单引号、双引号、分号、注释符、反引号、换行符等
        import re
        identifier_pattern = r'^[a-zA-Z0-9_\.\-\:\s]+$'
        forbidden_chars = ["'", '"', ';', '--', '/*', '*/', '`', '\n', '\r', '\x00']

        def is_safe_identifier(identifier: str) -> bool:
            """检查标识符是否安全（不包含危险字符）"""
            if not re.match(identifier_pattern, identifier):
                return False
            for char in forbidden_chars:
                if char in identifier:
                    return False
            return True

        if not is_safe_identifier(table_name):
            logger.warning(f"表名格式验证失败: '{table_name}'")
            raise ValueError(f"表名格式无效: '{table_name}'")
        if not is_safe_identifier(schema_name):
            logger.warning(f"Schema名格式验证失败: '{schema_name}'")
            raise ValueError(f"Schema名格式无效: '{schema_name}'")
        if not is_safe_identifier(column_name):
            logger.warning(f"列名格式验证失败: '{column_name}'")
            raise ValueError(f"列名格式无效: '{column_name}'")

        # 记录审计日志
        logger.debug(f"获取列的不同值: schema={schema_name}, table={table_name}, column={column_name}, max_num={max_num}")

        query = text(f"""
            SELECT DISTINCT "{column_name}"
            FROM "{schema_name}"."{table_name}"
            WHERE "{column_name}" IS NOT NULL
            LIMIT :max_num
        """)
        with self._engine.connect() as conn:
            result = conn.execute(query, {"max_num": max_num})
            return [row[0] for row in result if row[0] is not None]
    
    def fetch(self, sql_query: str) -> Tuple[bool, Any]:
        """执行 SQL 查询"""
        sql_query = preprocess_sql_query(sql_query)
        validate_sql_query(sql_query)

        try:
            columns, records = _run_query_with_timeout(self._engine, sql_query)
            records = [tuple(row) for row in records]
            return True, (records, columns)
        except TimeoutError as e:
            logger.warning(f"SQL fetch timeout: {sql_query[:200]}...")
            return False, str(e)
        except Exception as e:
            return False, str(e)
    
    def fetch_with_column_name(self, sql_query: str) -> Tuple[Any, List]:
        """执行查询并返回列名"""
        sql_query = preprocess_sql_query(sql_query)
        validate_sql_query(sql_query)

        try:
            columns, records = _run_query_with_timeout(self._engine, sql_query)
            return records, columns
        except TimeoutError:
            logger.warning(f"SQL fetch_with_column_name timeout: {sql_query[:200]}...")
            return None, []
        except Exception:
            return None, []
    
    def fetch_truncated(self, sql_query: str, max_rows: Optional[int] = None, max_str_len: int = 30) -> Dict:
        """执行查询并截断结果"""
        sql_query = preprocess_sql_query(sql_query)
        validate_sql_query(sql_query)

        if max_rows is None:
            max_rows = 50000

        try:
            columns, records = _run_query_with_timeout(self._engine, sql_query)
            if max_rows:
                records = records[:max_rows]
            truncated_results = [
                tuple(self._truncate_word(col, length=max_str_len) for col in row)
                for row in records
            ]
            return {"truncated_results": truncated_results, "fields": columns}
        except TimeoutError as e:
            logger.warning(f"SQL fetch_truncated timeout: {sql_query[:200]}...")
            return {"truncated_results": str(e), "fields": []}
        except Exception as e:
            return {"truncated_results": str(e), "fields": []}
    
    def _truncate_word(self, content: Any, length: int = 30) -> str:
        """截断字符串"""
        if content is None:
            return ""
        content_str = str(content)
        if len(content_str) > length:
            return content_str[:length] + "..."
        return content_str
    
    def trunc_result_to_markdown(self, sql_res: Dict) -> str:
        """将查询结果转换为 Markdown 表格"""
        truncated_results = sql_res.get("truncated_results", [])
        fields = sql_res.get("fields", [])

        if not isinstance(truncated_results, list):
            return str(truncated_results)

        header = "| " + " | ".join(fields) + " |"
        separator = "| " + " | ".join(["---"] * len(fields)) + " |"
        rows = []
        for row in truncated_results:
            rows.append("| " + " | ".join(str(value) for value in row) + " |")
        markdown_table = "\n".join([header, separator] + rows)
        return markdown_table
