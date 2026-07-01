"""DBConfig 数据类单元测试"""

import pytest
from urllib.parse import quote_plus

from xiyan_mcp_server.utils.db_config import DBConfig


class TestDBConfigDefaults:
    """各 dialect 的默认值填充逻辑"""

    def test_sqlite_default(self):
        cfg = DBConfig(dialect="sqlite")
        assert cfg.db_path == "book_1.sqlite"

    def test_sqlite_custom_path(self):
        cfg = DBConfig(dialect="sqlite", db_path="/tmp/my.db")
        assert cfg.db_path == "/tmp/my.db"

    def test_mysql_defaults(self):
        cfg = DBConfig(dialect="mysql")
        assert cfg.db_name == "default_db"
        assert cfg.user_name == "default_user"
        assert cfg.db_pwd == ""
        assert cfg.db_host == "localhost"
        assert cfg.port == 3306

    def test_postgresql_defaults(self):
        cfg = DBConfig(dialect="postgresql")
        assert cfg.db_name == "default_db"
        assert cfg.port == 5432

    def test_greptimedb_postgres_protocol(self):
        cfg = DBConfig(dialect="greptimedb")
        assert cfg.db_name == "public"
        assert cfg.user_name == "root"
        assert cfg.port == 4003

    def test_greptimedb_mysql_protocol(self):
        cfg = DBConfig(dialect="greptimedb_mysql")
        assert cfg.db_name == "public"
        assert cfg.user_name == "root"
        assert cfg.port == 4002


class TestDBConfigPasswordEncoding:
    """特殊字符密码应被 URL 编码"""

    def test_mysql_password_encoded(self):
        cfg = DBConfig(dialect="mysql", db_pwd="p@ss w0rd!")
        assert cfg.db_pwd == quote_plus("p@ss w0rd!")

    def test_greptimedb_password_encoded(self):
        cfg = DBConfig(dialect="greptimedb", db_pwd="a/b+c")
        assert cfg.db_pwd == quote_plus("a/b+c")

    def test_username_encoded(self):
        cfg = DBConfig(dialect="mysql", user_name="user@domain")
        assert cfg.user_name == quote_plus("user@domain")


class TestDBConfigUnsupportedDialect:

    def test_raises_on_unknown_dialect(self):
        with pytest.raises(ValueError, match="Unsupported database dialect"):
            DBConfig(dialect="oracle")
