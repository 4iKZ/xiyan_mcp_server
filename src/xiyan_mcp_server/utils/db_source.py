import asyncio
import concurrent.futures
import os
import threading
from typing import Any, Dict, List, Optional, Tuple
import logging
import sqlparse
from sqlparse.sql import Statement, IdentifierList, Identifier
from sqlparse.tokens import Keyword, DML

logger = logging.getLogger("xiyan_mcp_server.db_source")

SQL_EXECUTE_TIMEOUT = int(os.getenv("SQL_EXECUTE_TIMEOUT", "60"))

# 全局 SQL 执行线程池（复用，避免每次查询创建/销毁线程开销）
_sql_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
_sql_executor_lock = threading.Lock()


def get_sql_executor() -> concurrent.futures.ThreadPoolExecutor:
    """获取全局 SQL 执行线程池（线程安全，延迟初始化）"""
    global _sql_executor
    if _sql_executor is None:
        with _sql_executor_lock:
            if _sql_executor is None:
                max_workers = int(os.getenv("SQL_THREAD_POOL_SIZE", "8"))
                _sql_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="sql-exec",
                )
                logger.info(f"SQL 执行线程池已初始化: max_workers={max_workers}")
    return _sql_executor


def shutdown_sql_executor() -> None:
    """关闭全局 SQL 执行线程池（供服务器关闭时调用）"""
    global _sql_executor
    with _sql_executor_lock:
        if _sql_executor is not None:
            _sql_executor.shutdown(wait=False)
            _sql_executor = None
            logger.info("SQL 执行线程池已关闭")


def _do_query(engine, sql_query: str):
    """在独立连接中执行 SQL 查询（供线程池 submit 使用）"""
    with engine.begin() as conn:
        cursor = conn.execute(text(sql_query))
        columns = list(cursor.keys())
        records = [tuple(row) for row in cursor.fetchall()]
        return columns, records


def _run_query_with_timeout(engine, sql_query: str, timeout: int = SQL_EXECUTE_TIMEOUT):
    """在全局线程池中执行 SQL 查询，超时抛出 TimeoutError。

    使用全局共享的 ThreadPoolExecutor，避免每次查询创建/销毁线程。
    超时时通过 future.cancel() 取消任务（线程会自然结束于 SQL 执行完成）。
    """
    executor = get_sql_executor()
    future = executor.submit(_do_query, engine, sql_query)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        logger.warning(
            f"SQL 执行超时 ({timeout}s)，线程仍占用池槽位: {sql_query[:200]}"
        )
        raise TimeoutError(f"SQL 执行超时 ({timeout}s)")

from llama_index.core import SQLDatabase
from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.engine import Engine

from .db_mschema import MSchema
from .db_util import examples_to_str, preprocess_sql_query


def validate_sql_query(sql_query: str, allow_multiple: bool = False) -> None:
    """
    验证 SQL 查询的安全性。

    Args:
        sql_query: 要验证的 SQL 查询
        allow_multiple: 是否允许多个语句（默认 False）

    Raises:
        ValueError: 如果 SQL 不安全
    """
    # 检查是否跳过验证（仅用于测试/调试）
    if os.getenv("SKIP_SQL_VALIDATION", "").lower() in ("1", "true", "yes"):
        return

    if not sql_query or not sql_query.strip():
        raise ValueError("SQL 查询为空")

    # 去除注释和空格后检查是否为空
    stripped_sql = ' '.join(line.split('--')[0].split('#')[0] for line in sql_query.split('\n'))
    if not stripped_sql.strip():
        raise ValueError("SQL 查询为空或仅包含注释")

    # 解析 SQL
    parsed = sqlparse.parse(sql_query)

    if not parsed:
        raise ValueError("无法解析 SQL 查询")

    if not allow_multiple and len(parsed) > 1:
        logger.warning(f"SQL validation failed: Multiple statements detected. Query: {sql_query}")
        raise ValueError("只允许单个 SQL 语句")

    for statement in parsed:
        # 获取语句类型
        stmt_type = statement.get_type()

        # 只允许 SELECT 查询
        if stmt_type != 'SELECT' and stmt_type != 'UNKNOWN':
            # UNKNOWN 类型可能是 SELECT，需要进一步检查
            sql_upper = sql_query.upper()
            if not sql_upper.strip().startswith('SELECT'):
                logger.warning(f"SQL validation failed: Non-SELECT statement ({stmt_type}). Query: {sql_query}")
                raise ValueError(f"不允许的 SQL 类型: {stmt_type}，只允许 SELECT 查询")

        # 对于 UNKNOWN 类型，也需要检查是否包含实际的 SQL 关键词
        if stmt_type == 'UNKNOWN':
            # 获取去除注释的 SQL
            cleaned_sql = ' '.join(
                token.value for token in statement.flatten()
                if token.ttype is not sqlparse.tokens.Comment
                and token.ttype is not sqlparse.tokens.Comment.Single
                and token.ttype is not sqlparse.tokens.Comment.Multiline
            ).strip()

            if not cleaned_sql or not any(keyword in cleaned_sql.upper() for keyword in ['SELECT', 'FROM', 'WHERE']):
                raise ValueError("SQL 查询无效或仅包含注释")

        # 检查危险关键词（双重检查）
        sql_upper = sql_query.upper()
        dangerous_keywords = [
            'DROP', 'DELETE', 'UPDATE', 'INSERT', 'TRUNCATE',
            'ALTER', 'CREATE', 'GRANT', 'REVOKE', 'EXECUTE',
            'CALL', 'DECLARE', 'CURSOR', 'MERGE', 'REPLACE'
        ]
        for keyword in dangerous_keywords:
            # 检查关键词是否作为独立 token 出现
            if f' {keyword} ' in f' {sql_upper} ':
                raise ValueError(f"不允许使用关键词: {keyword}")


class HITLSQLDatabase(SQLDatabase):
    def __init__(self, engine: Engine, schema: Optional[str] = None, metadata: Optional[MetaData] = None,
                 ignore_tables: Optional[List[str]] = None, include_tables: Optional[List[str]] = None,
                 sample_rows_in_table_info: int = 3, indexes_in_table_info: bool = False,
                 custom_table_info: Optional[dict] = None, view_support: bool = False, max_string_length: int = 300,
                 mschema: Optional[MSchema] = None, db_name: Optional[str] = ''):
        super().__init__(engine, schema, metadata, ignore_tables, include_tables, sample_rows_in_table_info,
                         indexes_in_table_info, custom_table_info, view_support, max_string_length)

        self._db_name = db_name
        self._usable_tables = [table_name for table_name in self._usable_tables if self._inspector.has_table(table_name, schema)]
        self._dialect = engine.dialect.name
        if mschema is not None:
            self._mschema = mschema
        else:
            self._mschema = MSchema(db_id=db_name, schema=schema)
            self.init_mschema()

    @property
    def mschema(self) -> MSchema:
        """Return M-Schema"""
        return self._mschema

    @property
    def db_name(self) -> str:
        """Return db_name"""
        return self._db_name

    def get_pk_constraint(self, table_name: str) -> Dict:
        return self._inspector.get_pk_constraint(table_name, self._schema)['constrained_columns']

    def get_table_comment(self, table_name: str):
        """获取表注释，SQLite 等不支持时返回空字符串"""
        try:
            return self._inspector.get_table_comment(table_name, self._schema)['text']
        except NotImplementedError:
            # 预期异常：数据库不支持注释（如 SQLite）
            logger.debug(f"表注释获取不支持: {table_name}")
            return ''
        except (KeyError, AttributeError) as e:
            # 预期异常：返回格式不符合预期
            logger.debug(f"表注释格式异常: {table_name}: {e}")
            return ''
        except Exception as e:
            # 非预期异常：记录警告但继续
            logger.warning(f"获取表注释失败 {table_name}: {e}")
            return ''

    def default_schema_name(self) -> Optional[str]:
        return self._inspector.default_schema_name

    def get_schema_names(self) -> List[str]:
        return self._inspector.get_schema_names()

    def get_foreign_keys(self, table_name: str):
        return self._inspector.get_foreign_keys(table_name, self._schema)

    def get_unique_constraints(self, table_name: str):
        return self._inspector.get_unique_constraints(table_name, self._schema)
    
    def fectch_distinct_values(self, table_name: str, column_name: str, max_num: int = 5):
        table = Table(table_name, self.metadata_obj, autoload_with=self._engine)
        # 构建 SELECT DISTINCT 查询
        query = select(table.c[column_name]).distinct().limit(max_num)
        values = []
        with self._engine.connect() as connection:
            result = connection.execute(query)
            distinct_values = result.fetchall()
            for value in distinct_values:
                if value[0] is not None and value[0] != '':
                    values.append(value[0])
        return values
    
    def fetch(self, sql_query: str, max_rows: int = 10000):
        """
        执行 SQL 查询，返回结果（同步接口，保留兼容）

        Args:
            sql_query: SQL 查询语句
            max_rows: 最大返回行数（防止内存溢出，默认 10000）

        Returns:
            (status, records): status 为 True 时返回数据列表，False 时返回错误信息
        """
        sql_query = preprocess_sql_query(sql_query)
        validate_sql_query(sql_query)
        logger.debug(f"Executing SQL fetch: {sql_query}")

        try:
            columns, records = _run_query_with_timeout(self._engine, sql_query)

            if len(records) > max_rows:
                logger.warning(
                    f"查询结果超过 {max_rows} 行，已截断。"
                    f"建议使用 LIMIT 或 WHERE 子句减少数据量。"
                )
                records = records[:max_rows]

            logger.info(f"SQL fetch successful, rows: {len(records)}")
            return True, (records, columns)
        except TimeoutError as e:
            logger.warning(f"SQL fetch timeout: {sql_query[:200]}...")
            return False, str(e)
        except Exception as e:
            logger.error(f"SQL fetch error: {e}")
            return False, str(e)

    async def fetch_async(self, sql_query: str, *, deadline, max_rows: int = 10000):
        """异步执行 SQL 查询（通过 SqlExecutionManager）

        Args:
            sql_query: SQL 查询语句
            deadline: Deadline 实例（绝对截止时间）
            max_rows: 最大返回行数

        Returns:
            (status, records): 与 fetch() 格式一致
        """
        from .sql_executor import get_sql_manager

        sql_query = preprocess_sql_query(sql_query)
        validate_sql_query(sql_query)
        logger.debug(f"Executing SQL fetch_async: {sql_query}")

        manager = get_sql_manager()
        if manager is None:
            # 降级到同步路径（兼容未初始化 manager 的场景）
            return self.fetch(sql_query, max_rows)

        try:
            result = await manager.execute_async(
                self._engine, sql_query, deadline=deadline, max_rows=max_rows
            )
            logger.info(f"SQL fetch_async successful, rows: {result.row_count}")
            return True, (result.records, result.columns)
        except Exception as e:
            from ..runtime import DeadlineExceeded
            if isinstance(e, DeadlineExceeded):
                logger.warning(f"SQL fetch_async deadline exceeded: {sql_query[:200]}...")
            else:
                logger.error(f"SQL fetch_async error: {e}")
            return False, str(e)

    def fetch_with_column_name(self, sql_query: str):
        sql_query = preprocess_sql_query(sql_query)
        validate_sql_query(sql_query)
        logger.debug(f"Executing SQL fetch_with_column_name: {sql_query}")

        try:
            columns, records = _run_query_with_timeout(self._engine, sql_query)
            logger.info(f"SQL fetch_with_column_name successful, rows: {len(records)}")
            return records, columns
        except TimeoutError:
            logger.warning(f"SQL fetch_with_column_name timeout: {sql_query[:200]}...")
            return None, []
        except Exception as e:
            logger.error(f"SQL fetch_with_column_name error: {e}")
            return None, []

    def fetch_with_error_info(self, sql_query: str) -> Tuple[List, str]:
        info = ''
        sql_query = preprocess_sql_query(sql_query)
        with self._engine.begin() as connection:
            try:
                cursor = connection.execute(text(sql_query))
                records = cursor.fetchall()
            except Exception as e:
                info = str(e)
                records = None
        return records, info

    def fetch_truncated(self, sql_query: str, max_rows: Optional[int] = None, max_str_len: int = 30) -> Dict:
        """执行查询并截断结果，支持大数据集"""
        sql_query = preprocess_sql_query(sql_query)
        validate_sql_query(sql_query)
        logger.debug(f"Executing SQL fetch_truncated: {sql_query}")

        # 默认最大行数为 50000，防止内存溢出
        if max_rows is None:
            max_rows = 50000

        try:
            columns, records = _run_query_with_timeout(self._engine, sql_query)
            if max_rows:
                records = records[:max_rows]
            truncated_results = [
                tuple(self.truncate_word(col, length=max_str_len) for col in row)
                for row in records
            ]
            logger.info(f"SQL fetch_truncated successful, rows: {len(truncated_results)}")
            return {"truncated_results": truncated_results, "fields": columns}

        except TimeoutError as e:
            logger.warning(f"SQL fetch_truncated timeout: {sql_query[:200]}...")
            return {"truncated_results": str(e), "fields": []}
        except Exception as e:
            logger.error(f"SQL fetch_truncated error: {e}")
            return {"truncated_results": str(e), "fields": []}

    def trunc_result_to_markdown(self, sql_res: Dict) -> str:
        """
        数据库查询结果转换成markdown格式
        """
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
    

    def execute(self, sql_query: str, timeout=5) -> Any:
        # import concurrent.futures
        sql_query = preprocess_sql_query(sql_query)

        with self._engine.begin() as connection:
            try:
                cursor = connection.execute(text(sql_query))
                return True
            except Exception as e:
                info = str(e)
                print("SQL执行异常：", info)
                return None

    def init_mschema(self):
        for table_name in self._usable_tables:
            table_comment = self.get_table_comment(table_name)
            table_comment = '' if table_comment is None else table_comment.strip()
            self._mschema.add_table(table_name, fields={}, comment=table_comment)
            pks = self.get_pk_constraint(table_name)

            fks = self.get_foreign_keys(table_name)
            for fk in fks:
                referred_schema = fk['referred_schema']
                for c, r in zip(fk['constrained_columns'], fk['referred_columns']):
                    self._mschema.add_foreign_key(table_name, c, referred_schema, fk['referred_table'], r)

            fields = self._inspector.get_columns(table_name, schema=self._schema)
            for field in fields:
                field_type = f"{field['type']!s}"
                field_name = field['name']
                if field_name in pks:
                    primary_key = True
                else:
                    primary_key = False

                field_comment = field.get("comment", None)
                field_comment = "" if field_comment is None else field_comment.strip()
                autoincrement = field.get('autoincrement', False)
                default = field.get('default', None)
                if default is not None:
                    default = f'{default}'

                try:
                    examples = self.fectch_distinct_values(table_name, field_name, 5)
                except Exception as e:
                    # 区分可恢复和不可恢复错误
                    if "does not exist" in str(e) or "no such table" in str(e):
                        logger.debug(f"表不存在，跳过示例值: {table_name}.{field_name}")
                    else:
                        logger.warning(f"获取列示例值失败 {table_name}.{field_name}: {e}")
                    examples = []
                examples = examples_to_str(examples)

                self._mschema.add_field(table_name, field_name, field_type=field_type, primary_key=primary_key,
                    nullable=field['nullable'], default=default, autoincrement=autoincrement,
                    comment=field_comment, examples=examples)

    def sync_to_local(self, local_engine: Engine):
        """同步数据到本地数据库"""
        from sqlalchemy.orm import sessionmaker

        local_metadata = MetaData()

        # # 连接到远程数据库
        remote_metadata = MetaData()
        remote_metadata.reflect(bind=self._engine)

        remote_metadata.create_all(bind=self._engine)

        print(remote_metadata.tables.keys())
        # 同步表结构和数据
        for table_name in remote_metadata.tables:
            remote_table = Table(table_name, remote_metadata, autoload_with=self._engine)
            print(f"Syncing table {table_name}...")

            # 创建本地表
            remote_table.metadata = local_metadata
            local_metadata.drop_all(local_engine)
            local_metadata.create_all(local_engine, tables=[remote_table])

            # 将数据同步到本地（修复：显式管理 Session）
            Session = sessionmaker(bind=self._engine)
            session = Session()
            try:
                with local_engine.begin() as local_connection:
                    data = session.query(remote_table).all()
                    columns = remote_table.columns.keys()
                    insert_data = [dict(zip(columns, d)) for d in data]
                    local_connection.execute(remote_table.insert(), insert_data)
            finally:
                session.close()  # 显式关闭 Session
                logger.info(f"表 {table_name} 同步完成")

        print("Sync complete.")


