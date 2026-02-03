from dataclasses import dataclass, field
from typing import Optional

from urllib.parse import quote_plus
@dataclass
class DBConfig:
    dialect: str = 'sqlite'
    db_path: Optional[str] = None  # 仅用于 SQLite
    db_name: Optional[str] = None  # MySQL/PostgreSQL 通用
    user_name: Optional[str] = None  # MySQL/PostgreSQL 通用
    db_pwd: Optional[str] = None  # MySQL/PostgreSQL 通用
    db_host: Optional[str] = None  # MySQL/PostgreSQL 通用
    port: Optional[int] = None  # MySQL/PostgreSQL 通用

    def __post_init__(self):
        if self.dialect == 'sqlite':
            self.db_path = self.db_path or 'book_1.sqlite'
        elif self.dialect in ['mysql', 'postgresql']:
            self.db_name = self.db_name or 'default_db'
            self.user_name = quote_plus(self.user_name) if self.user_name else 'default_user'
            self.db_pwd = quote_plus(self.db_pwd) if self.db_pwd else ''
            self.db_host = self.db_host or 'localhost'
            self.port = self.port or (3306 if self.dialect == 'mysql' else 5432)
        elif self.dialect == 'greptimedb':
            # GreptimeDB 使用自定义方言，通过 PostgreSQL 协议连接
            self.db_name = self.db_name or 'public'
            self.user_name = quote_plus(self.user_name) if self.user_name else 'root'
            self.db_pwd = quote_plus(self.db_pwd) if self.db_pwd else ''
            self.db_host = self.db_host or 'localhost'
            self.port = self.port or 4003  # GreptimeDB PostgreSQL 协议默认端口
        elif self.dialect == 'greptimedb_mysql':
            # GreptimeDB 使用自定义方言，通过 MySQL 协议连接
            self.db_name = self.db_name or 'public'
            self.user_name = quote_plus(self.user_name) if self.user_name else 'root'
            self.db_pwd = quote_plus(self.db_pwd) if self.db_pwd else ''
            self.db_host = self.db_host or 'localhost'
            self.port = self.port or 4002  # GreptimeDB MySQL 协议默认端口
        else:
            raise ValueError(f"Unsupported database dialect: {self.dialect}")