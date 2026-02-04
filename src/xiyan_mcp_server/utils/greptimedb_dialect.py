"""
GreptimeDB 专用 SQLAlchemy 方言

GreptimeDB 支持 PostgreSQL 协议，但其 pg_catalog 实现不完整（缺少 typnamespace 等字段）。
此方言通过以下方式绕过兼容性问题：
1. 禁用类型缓存，避免查询 pg_type
2. 使用 INFORMATION_SCHEMA 替代 pg_catalog 获取元数据
3. 对不支持的功能返回空值
"""

from sqlalchemy.dialects.postgresql import psycopg2 as postgresql_psycopg2
from sqlalchemy.dialects.postgresql.base import PGDialect, PGInspector
from sqlalchemy.engine import reflection
from sqlalchemy import text
import logging

logger = logging.getLogger("greptimedb_dialect")


class GreptimeDBInspector(PGInspector):
    """
    GreptimeDB 专用 Inspector，使用 INFORMATION_SCHEMA 获取元数据
    """

    def _get_default_schema(self):
        """获取默认 schema（GreptimeDB 中 schema 等于数据库名）"""
        # 从连接 URL 中提取数据库名
        try:
            # self.bind 可能是 Engine 或 Connection
            if hasattr(self.bind, 'engine'):
                url = self.bind.engine.url
            else:
                url = self.bind.url
            db_name = url.database
            return db_name if db_name else "public"
        except Exception:
            return "public"

    def get_table_names(self, schema=None, **kw):
        """使用 INFORMATION_SCHEMA 获取表列表"""
        schema = schema or self._get_default_schema()
        query = text("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = :schema
            AND table_type = 'BASE TABLE'
        """)
        with self.bind.connect() as conn:
            result = conn.execute(query, {"schema": schema})
            return [row[0] for row in result]


    def get_columns(self, table_name, schema=None, **kw):
        """使用 INFORMATION_SCHEMA 获取列信息"""
        schema = schema or self._get_default_schema()
        query = text("""
            SELECT 
                column_name,
                data_type,
                is_nullable,
                column_default,
                character_maximum_length
            FROM information_schema.columns 
            WHERE table_schema = :schema
            AND table_name = :table_name
            ORDER BY ordinal_position
        """)
        with self.bind.connect() as conn:
            result = conn.execute(query, {"schema": schema, "table_name": table_name})
            columns = []
            for row in result:
                col_name, data_type, is_nullable, default, max_length = row
                # 映射 GreptimeDB 类型到标准类型
                col_type = self._map_greptimedb_type(data_type, max_length)
                columns.append({
                    "name": col_name,
                    "type": col_type,
                    "nullable": is_nullable == "YES",
                    "default": default,
                    "autoincrement": False,
                    "comment": "",
                })
            return columns

    def _map_greptimedb_type(self, data_type, max_length=None):
        """将 GreptimeDB 类型映射为 SQLAlchemy 类型字符串"""
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
        mapped = type_mapping.get(type_upper, type_upper)
        if mapped == "VARCHAR" and max_length:
            return f"VARCHAR({max_length})"
        return mapped

    def get_pk_constraint(self, table_name, schema=None, **kw):
        """GreptimeDB 主键约束 - 尝试从 time_index 推断"""
        # GreptimeDB 使用 time_index 作为主键概念
        return {"constrained_columns": [], "name": None}

    def get_foreign_keys(self, table_name, schema=None, **kw):
        """GreptimeDB 不支持外键"""
        return []

    def get_unique_constraints(self, table_name, schema=None, **kw):
        """GreptimeDB 不支持唯一约束"""
        return []

    def get_indexes(self, table_name, schema=None, **kw):
        """GreptimeDB 索引信息"""
        return []

    def get_table_comment(self, table_name, schema=None, **kw):
        """GreptimeDB 不支持表注释"""
        return {"text": None}

    def has_table(self, table_name, schema=None, **kw):
        """检查表是否存在"""
        schema = schema or self._get_default_schema()
        query = text("""
            SELECT 1 FROM information_schema.tables 
            WHERE table_schema = :schema AND table_name = :table_name
        """)
        with self.bind.connect() as conn:
            result = conn.execute(query, {"schema": schema, "table_name": table_name})
            return result.fetchone() is not None


class GreptimeDBDialect(postgresql_psycopg2.PGDialect_psycopg2):
    """
    GreptimeDB 专用方言

    继承 PostgreSQL psycopg2 方言，但禁用类型反射以避免 pg_catalog 兼容性问题
    """

    name = "greptimedb"

    # 禁用类型缓存，避免查询 pg_type
    supports_native_enum = False
    supports_native_boolean = True

    inspector = GreptimeDBInspector

    def initialize(self, connection):
        """初始化连接，跳过类型加载"""
        # 设置基本属性，避免调用父类的 initialize 触发 pg_type 查询
        self.server_version_info = (14, 0)  # 模拟 PostgreSQL 14
        self.default_schema_name = "public"
        self._supports_create_index_concurrently = False
        self._supports_drop_index_concurrently = False
        self.implicit_returning = True
        self.supports_smallserial = True
        self.supports_native_decimal = True
        self._backslash_escapes = True

        # 设置 psycopg2 特定属性
        self.psycopg2_version = (2, 9)
        self._has_native_hstore = False
        self._has_native_json = False
        self._has_native_jsonb = False

    def _get_server_version_info(self, connection):
        """返回模拟的服务器版本"""
        return (14, 0)

    def get_isolation_level(self, dbapi_connection):
        """获取隔离级别"""
        return "AUTOCOMMIT"

    def get_default_isolation_level(self, dbapi_conn):
        return "AUTOCOMMIT"

    def _get_default_schema_name(self, connection):
        return "public"

    def on_connect(self):
        """连接时的回调，返回空函数列表以跳过 hstore 检测"""
        # 返回空列表，跳过所有 psycopg2 扩展注册
        return []


# 注册方言 - 只注册基础方言名
from sqlalchemy.dialects import registry
registry.register("greptimedb", "xiyan_mcp_server.utils.greptimedb_dialect", "GreptimeDBDialect")

