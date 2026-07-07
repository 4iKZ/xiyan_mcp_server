"""query_tracker 单元测试

覆盖纯函数：classify_error, extract_tables_from_sql,
extract_relevant_schema, count_available_tables, QueryTracker._sanitize_tag
"""

import json
import pytest
from pathlib import Path

from xiyan_mcp_server.utils.query_tracker import (
    classify_error,
    extract_tables_from_sql,
    extract_relevant_schema,
    count_available_tables,
    QueryTracker,
)


# ── classify_error ─────────────────────────────────────────────

class TestClassifyError:

    def test_empty_returns_unknown(self):
        assert classify_error("") == "unknown"
        assert classify_error(None) == "unknown"

    def test_timeout(self):
        assert classify_error("query timed out after 30s") == "timeout"
        assert classify_error("查询超时") == "timeout"

    def test_permission_denied(self):
        assert classify_error("Access denied for user 'root'") == "permission_denied"

    def test_connection_error(self):
        assert classify_error("Connection refused") == "connection_error"

    def test_column_not_found(self):
        assert classify_error("Unknown column 'foo' in 'field list'") == "column_not_found"
        assert classify_error("列不存在") == "column_not_found"

    def test_table_not_found(self):
        assert classify_error("Table 'users' doesn't exist") == "table_not_found"
        assert classify_error("表不存在") == "table_not_found"

    def test_syntax_error(self):
        assert classify_error("syntax error at position 42") == "syntax_error"
        assert classify_error("语法错误") == "syntax_error"

    def test_type_error(self):
        assert classify_error("cannot coerce INT to VARCHAR") == "type_error"
        assert classify_error("failed to coerce type") == "type_error"

    def test_function_not_found(self):
        assert classify_error("function foo(int) does not exist") == "function_not_found"
        assert classify_error("invalid function bar") == "function_not_found"

    def test_join_error(self):
        assert classify_error("ambiguous reference to column 'id'") == "join_error"
        assert classify_error("JOIN condition mismatch") == "join_error"

    def test_unsupported_statement(self):
        assert classify_error("feature not supported: DELETE") == "unsupported_statement"

    def test_unsupported_statement_merge(self):
        """MERGE 关键词应归类为 unsupported_statement（修复前归类为 other）"""
        assert classify_error("不允许使用关键词: MERGE") == "unsupported_statement"
        assert classify_error("keyword MERGE is not allowed") == "unsupported_statement"

    def test_validation_empty_sql_to_syntax_error(self):
        """空 SQL 验证错误应归类为 syntax_error，进入 3 次重试循环"""
        assert classify_error("SQL 查询为空") == "syntax_error"
        assert classify_error("SQL 查询为空或仅包含注释") == "syntax_error"
        assert classify_error("SQL 查询无效或仅包含注释") == "syntax_error"
        assert classify_error("LLM 生成的 SQL 为空，需重新生成") == "syntax_error"

    def test_function_not_found_variance(self):
        """variance 函数不存在 → function_not_found"""
        assert classify_error("Invalid function 'variance'. Did you mean 'radians'?") == "function_not_found"
        assert classify_error("function variance not supported") == "function_not_found"

    def test_type_coercion_approx_percentile(self):
        """approx_percentile_cont 参数类型不匹配 → type_error"""
        assert classify_error(
            "Failed to coerce arguments to satisfy a call to 'approx_percentile_cont'"
        ) == "type_error"

    def test_planner_error(self):
        assert classify_error("failed to plan query") == "planner_error"

    def test_object_not_exist(self):
        assert classify_error("foobar does not exist") == "object_not_found"

    def test_other(self):
        assert classify_error("some random error message") == "other"

    # ── 本次优化新增测试 ──

    def test_no_field_named_to_column_not_found(self):
        """DataFusion 'No field named' 应归类为 column_not_found"""
        assert classify_error("No field named 'greptime_value'") == "column_not_found"
        assert classify_error("field named 'foo' not found") == "column_not_found"

    def test_distinct_orderby_classification(self):
        """SELECT DISTINCT + ORDER BY 不在 SELECT 列表 → distinct_orderby_error"""
        assert classify_error("column must appear in select list for select distinct") == "distinct_orderby_error"
        assert classify_error("for select distinct order by") == "distinct_orderby_error"

    def test_sql_truncation_prevents_misclassification(self):
        """错误消息中 [SQL: ...] 部分的 SQL 关键词不应影响分类"""
        # SQL 中的 "my_table" 含 "table"，不应误分类为 table_not_found
        err = "syntax error at position 42. [SQL: SELECT * FROM my_table WHERE x = 1]"
        assert classify_error(err) == "syntax_error"
        # planner 错误中的 SQL 含 "table"，不应误分类
        err = "failed to plan query. [SQL: SELECT * FROM my_table]"
        assert classify_error(err) == "planner_error"


# ── extract_tables_from_sql ────────────────────────────────────

class TestExtractTablesFromSql:

    def test_simple_select(self):
        tables = extract_tables_from_sql("SELECT * FROM users")
        assert tables == ["users"]

    def test_join(self):
        sql = "SELECT * FROM users JOIN orders ON users.id = orders.user_id"
        tables = extract_tables_from_sql(sql)
        assert "users" in tables
        assert "orders" in tables

    def test_subquery(self):
        sql = "SELECT * FROM (SELECT id FROM orders) AS sub JOIN users ON sub.id = users.id"
        tables = extract_tables_from_sql(sql)
        assert "users" in tables
        assert "orders" in tables

    def test_insert_into(self):
        sql = "INSERT INTO audit_log (msg) VALUES ('hello')"
        tables = extract_tables_from_sql(sql)
        assert "audit_log" in tables

    def test_empty_sql(self):
        assert extract_tables_from_sql("") == []
        assert extract_tables_from_sql(None) == []

    def test_schema_qualified_table(self):
        sql = "SELECT * FROM public.users"
        tables = extract_tables_from_sql(sql)
        assert "public.users" in tables

    def test_case_insensitive(self):
        sql = "select * from Users where ID in (select user_id from Orders)"
        tables = extract_tables_from_sql(sql)
        assert "Users" in tables or "users" in [t.lower() for t in tables]


# ── extract_relevant_schema ────────────────────────────────────

class TestExtractRelevantSchema:

    def test_extracts_matching_tables(self, sample_schema_text):
        result = extract_relevant_schema(sample_schema_text, ["abortspanbytes"])
        assert "abortspanbytes" in result
        assert "sys_cpu_usage" not in result

    def test_multiple_tables(self, sample_schema_text):
        result = extract_relevant_schema(sample_schema_text, ["abortspanbytes", "gc_count"])
        assert "abortspanbytes" in result
        assert "gc_count" in result
        assert "sys_cpu_usage" not in result

    def test_empty_inputs(self):
        assert extract_relevant_schema("", ["users"]) == ""
        assert extract_relevant_schema("# Table: x\n[]", []) == ""

    def test_schema_qualified_name(self, sample_schema_text):
        """db.table 格式应能通过短名匹配"""
        result = extract_relevant_schema(sample_schema_text, ["mydb.abortspanbytes"])
        assert "abortspanbytes" in result


# ── count_available_tables ─────────────────────────────────────

class TestCountAvailableTables:

    def test_counts_tables(self, sample_schema_text):
        assert count_available_tables(sample_schema_text) == 3

    def test_empty_schema(self):
        assert count_available_tables("") == 0
        assert count_available_tables(None) == 0


# ── QueryTracker._sanitize_tag ─────────────────────────────────

class TestSanitizeTag:

    def test_safe_chars_preserved(self):
        assert QueryTracker._sanitize_tag("hello-world_v2.0") == "hello-world_v2.0"

    def test_unsafe_chars_replaced(self):
        result = QueryTracker._sanitize_tag("a/b c@d!")
        assert "/" not in result
        assert " " not in result

    def test_truncated_to_64(self):
        long_tag = "a" * 100
        assert len(QueryTracker._sanitize_tag(long_tag)) == 64


# ── QueryTracker 文件写入 ──────────────────────────────────────

class TestQueryTrackerWrite:

    def test_record_query_writes_jsonl(self, tmp_path):
        tracker = QueryTracker(output_dir=str(tmp_path))
        tracker.record_query(
            nl_query="测试查询",
            tool="get_data",
            database="testdb",
            dialect="mysql",
            initial_sql="SELECT 1",
            exec_success=True,
            exec_error=None,
            error_type=None,
            result_rows=1,
            result_preview=None,
            retry_count=0,
            retries=[],
            total_latency_ms=100.5,
            tables_used=["t1"],
            fields=["f1"],
            schema_filtered=False,
            available_table_count=10,
            relevant_schema="",
            filtered_table_names=[],
        )
        files = list(tmp_path.glob("query_tracker_*.jsonl"))
        assert len(files) == 1
        record = json.loads(files[0].read_text().strip())
        assert record["nl_query"] == "测试查询"
        assert record["exec_success"] is True

    def test_disabled_tracker_no_write(self, tmp_path):
        tracker = QueryTracker(output_dir=str(tmp_path))
        tracker.set_enabled(False)
        tracker.record_query(
            nl_query="x", tool="get_data", database="db", dialect="mysql",
            initial_sql="SELECT 1", exec_success=True, exec_error=None,
            error_type=None, result_rows=0, result_preview=None,
            retry_count=0, retries=[], total_latency_ms=0,
            tables_used=[], fields=[], schema_filtered=False,
            available_table_count=0, relevant_schema="",
            filtered_table_names=[],
        )
        files = list(tmp_path.glob("query_tracker_*.jsonl"))
        assert len(files) == 0
