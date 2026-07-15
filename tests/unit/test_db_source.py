"""db_source.py 单元测试（P0-GAP-09）"""

import os
import pytest
from unittest.mock import patch
from sqlalchemy import text

from xiyan_mcp_server.utils.db_source import validate_sql_query
from xiyan_mcp_server.utils.db_mschema import MSchema


class TestValidateSqlQuery:
    """SQL 安全校验"""

    @pytest.fixture(autouse=True)
    def _clear_skip_env(self, monkeypatch):
        monkeypatch.delenv("SKIP_SQL_VALIDATION", raising=False)

    def test_empty_sql_raises(self):
        with pytest.raises(ValueError, match="SQL 查询为空"):
            validate_sql_query("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="SQL 查询为空"):
            validate_sql_query("   ")

    def test_only_comment_raises(self):
        with pytest.raises(ValueError, match="仅包含注释"):
            validate_sql_query("-- this is a comment\n")

    def test_select_allowed(self):
        validate_sql_query("SELECT * FROM t")

    def test_select_with_unknown_type_allowed(self):
        validate_sql_query("SELECT 1")

    def test_drop_blocked_by_stmt_type(self):
        with pytest.raises(ValueError, match="不允许的 SQL 类型: DROP"):
            validate_sql_query("DROP TABLE t")

    def test_delete_blocked_by_stmt_type(self):
        with pytest.raises(ValueError, match="不允许的 SQL 类型: DELETE"):
            validate_sql_query("DELETE FROM t WHERE id = 1")

    def test_update_blocked_by_stmt_type(self):
        with pytest.raises(ValueError, match="不允许的 SQL 类型: UPDATE"):
            validate_sql_query("UPDATE t SET a = 1")

    def test_insert_blocked_by_stmt_type(self):
        with pytest.raises(ValueError, match="不允许的 SQL 类型: INSERT"):
            validate_sql_query("INSERT INTO t VALUES (1)")

    def test_multiple_statements_blocked(self):
        with pytest.raises(ValueError, match="只允许单个 SQL 语句"):
            validate_sql_query("SELECT 1; SELECT 2")

    def test_multiple_statements_allowed_when_flag(self):
        validate_sql_query("SELECT 1; SELECT 2", allow_multiple=True)

    def test_truncate_blocked_by_stmt_type(self):
        with pytest.raises(ValueError, match="不允许的 SQL 类型: TRUNCATE"):
            validate_sql_query("TRUNCATE TABLE t")

    def test_alter_blocked_by_stmt_type(self):
        with pytest.raises(ValueError, match="不允许的 SQL 类型: ALTER"):
            validate_sql_query("ALTER TABLE t ADD COLUMN x INT")

    def test_unparseable_sql_raises(self):
        with pytest.raises(ValueError, match="SQL 查询无效"):
            validate_sql_query("\x00\x01\x02")

    def test_unknown_non_select_raises(self):
        with pytest.raises(ValueError, match="SQL 查询无效"):
            validate_sql_query("SHOW TABLES")

    def test_keyword_in_comment_bypass(self):
        validate_sql_query("SELECT/*DROP*/TABLE t")

    def test_keyword_in_string_not_detected(self):
        validate_sql_query("SELECT 'DROP TABLE' AS col")

    def test_select_with_drop_in_subquery_allowed(self):
        validate_sql_query("SELECT * FROM t WHERE id IN (SELECT id FROM t2)")

    def test_skip_validation_env_skip(self, monkeypatch):
        monkeypatch.setenv("SKIP_SQL_VALIDATION", "1")
        validate_sql_query("DROP TABLE t")

    def test_skip_validation_env_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("SKIP_SQL_VALIDATION", "YES")
        validate_sql_query("DROP TABLE t")

    def test_skip_validation_env_true(self, monkeypatch):
        monkeypatch.setenv("SKIP_SQL_VALIDATION", "true")
        validate_sql_query("DROP TABLE t")
