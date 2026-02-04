import re
import os
import datetime, decimal
from sqlalchemy import create_engine, MetaData, Table, Column, String, Integer, select, text
from sqlalchemy.engine import Engine
from .db_config import DBConfig


def init_db_conn(db_config: DBConfig) -> Engine:
    if db_config.dialect.lower() == 'sqlite':
        return connect_to_sqlite(db_config.db_path)
    elif db_config.dialect.lower() == 'mysql':
        return connect_to_mysql(db_config.db_name, db_config.user_name, db_config.db_pwd, db_config.db_host, db_config.port)
    elif db_config.dialect.lower() == 'postgresql':
        return connect_to_pg(db_config.db_name, db_config.user_name, db_config.db_pwd, db_config.db_host, db_config.port)
    elif db_config.dialect.lower() == 'greptimedb':
        return connect_to_greptimedb(db_config.db_name, db_config.user_name, db_config.db_pwd, db_config.db_host, db_config.port)
    elif db_config.dialect.lower() == 'greptimedb_mysql':
        return connect_to_greptimedb_mysql(db_config.db_name, db_config.user_name, db_config.db_pwd, db_config.db_host, db_config.port)
    else:
        raise NotImplementedError


def connect_to_sqlite(db_path: str) -> Engine:
    assert os.path.exists(db_path)
    db_engine = create_engine(f'sqlite:///{os.path.abspath(db_path)}')
    return db_engine


def connect_to_mysql(db_name, user_name, db_pwd, db_host, port) -> Engine:
    db_engine = create_engine(f"mysql+pymysql://{user_name}:{db_pwd}@{db_host}:{port}/{db_name}")
    return db_engine


def connect_to_pg(db_name, user_name, db_pwd, db_host, port) -> Engine:
    db_engine = create_engine(f"postgresql+psycopg2://{user_name}:{db_pwd}@{db_host}:{port}/{db_name}")
    return db_engine


def connect_to_greptimedb(db_name, user_name, db_pwd, db_host, port) -> Engine:
    """
    连接到 GreptimeDB，使用自定义方言绕过 pg_catalog 兼容性问题
    """
    # 导入并注册 GreptimeDB 方言
    from . import greptimedb_dialect  # noqa: F401
    import psycopg2

    # 构建连接字符串，使用 greptimedb 方言（不带 +psycopg2）
    if db_pwd:
        conn_str = f"greptimedb://{user_name}:{db_pwd}@{db_host}:{port}/{db_name}"
    else:
        conn_str = f"greptimedb://{user_name}@{db_host}:{port}/{db_name}"

    # 使用 creator 函数绕过 psycopg2 的 hstore 检测
    def creator():
        # 直接创建 psycopg2 连接，不注册扩展
        if db_pwd:
            return psycopg2.connect(
                host=db_host, port=port, user=user_name,
                password=db_pwd, dbname=db_name
            )
        else:
            return psycopg2.connect(
                host=db_host, port=port, user=user_name,
                dbname=db_name
            )

    db_engine = create_engine(conn_str, creator=creator)
    return db_engine


def connect_to_greptimedb_mysql(db_name, user_name, db_pwd, db_host, port) -> Engine:
    """
    连接到 GreptimeDB，使用 MySQL 协议（端口 4002）
    MySQL 协议兼容性更好，不需要自定义方言
    """
    # 构建连接字符串，密码为空时不包含冒号
    if db_pwd:
        conn_str = f"mysql+pymysql://{user_name}:{db_pwd}@{db_host}:{port}/{db_name}"
    else:
        conn_str = f"mysql+pymysql://{user_name}@{db_host}:{port}/{db_name}"
    
    db_engine = create_engine(conn_str)
    return db_engine


def remove_sql_comments(sql_query: str) -> str:
    # 正则表达式用于匹配 SQL 注释
    single_line_comment_pattern = r'--[^\n]*'
    multi_line_comment_pattern = r'/\*.*?\*/'

    # 删除单行注释
    sql_without_single_comments = re.sub(single_line_comment_pattern, '', sql_query)

    # 删除多行注释
    sql_without_comments = re.sub(multi_line_comment_pattern, '', sql_without_single_comments, flags=re.DOTALL)

    return sql_without_comments.strip()


def preprocess_sql_query(sql_query: str) -> str:
    # 删除注释，加上分号
    sql_query = remove_sql_comments(sql_query)
    if not sql_query.strip().endswith(';'):
        sql_query += ';'
    return sql_query


def is_email(string):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    match = re.match(pattern, string)
    if match:
        return True
    else:
        return False


def examples_to_str(examples: list) -> list[str]:
    """
    from examples to a list of str
    """
    values = examples
    for i in range(len(values)):
        if isinstance(values[i], datetime.date):
            values = [values[i]]
            break
        elif isinstance(values[i], datetime.datetime):
            values = [values[i]]
            break
        elif isinstance(values[i], decimal.Decimal):
            values[i] = str(float(values[i]))
        elif is_email(str(values[i])):
            values = []
            break
        elif 'http://' in str(values[i]) or 'https://' in str(values[i]):
            values = []
            break
        elif values[i] is not None and not isinstance(values[i], str):
            pass
        elif values[i] is not None and '.com' in values[i]:
            pass

    return [str(v) for v in values if v is not None and len(str(v)) > 0]


def sql_fetcher(db_engine: Engine, sql_query: str):
    sql_query = preprocess_sql_query(sql_query)
    with db_engine.begin() as connection:
        try:
            cursor = connection.execute(text(sql_query))
            records = cursor.fetchall()
        except Exception as e:
            print("An exception occurred during SQL execution.\n", e)
            records = None
        return records