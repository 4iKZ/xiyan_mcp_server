"""file_util 单元测试"""

import json
import os
import pytest
from pathlib import Path

from xiyan_mcp_server.utils.file_util import (
    extract_sql_from_qwen,
    read_text,
    save_raw_text,
    read_json_file,
    write_json_to_file,
    save_as_csv,
    valid_path,
)


# ── extract_sql_from_qwen ──────────────────────────────────────

class TestExtractSqlFromQwen:
    """SQL 提取逻辑是 NL2SQL 管线的关键一环"""

    def test_plain_sql(self):
        sql = "SELECT * FROM users"
        assert extract_sql_from_qwen(sql) == sql

    def test_sql_in_code_block(self):
        text = "这是查询结果：\n```sql\nSELECT * FROM users\n```\n以上是结果"
        assert extract_sql_from_qwen(text) == "SELECT * FROM users"

    def test_multiple_code_blocks_takes_last(self):
        text = (
            "```sql\nSELECT 1\n```\n"
            "修正后：\n"
            "```sql\nSELECT * FROM orders WHERE id > 10\n```"
        )
        assert "orders" in extract_sql_from_qwen(text)

    def test_strips_sql_prefix_select(self):
        """LLM 有时会输出 'sql SELECT...' 前缀"""
        assert extract_sql_from_qwen("sql SELECT * FROM t") == "SELECT * FROM t"

    def test_strips_sql_prefix_with(self):
        assert extract_sql_from_qwen("sql WITH cte AS (SELECT 1) SELECT * FROM cte") == \
            "WITH cte AS (SELECT 1) SELECT * FROM cte"

    def test_sql_prefix_case_insensitive(self):
        assert extract_sql_from_qwen("SQL select * from t") == "select * from t"

    def test_multiline_sql_block(self):
        text = "```sql\nSELECT\n  a,\n  COUNT(*)\nFROM t\nGROUP BY a\n```"
        result = extract_sql_from_qwen(text)
        assert "COUNT(*)" in result
        assert "GROUP BY" in result

    def test_no_sql_prefix_no_block(self):
        """纯 SQL 不带代码块也不带前缀 → 原样返回"""
        sql = "SELECT id, name FROM users WHERE id = 1"
        assert extract_sql_from_qwen(sql) == sql

    def test_empty_string(self):
        assert extract_sql_from_qwen("") == ""

    def test_only_comment_response(self):
        """LLM 只返回注释/解释文字，无 SQL 代码块 → 返回原文（后续空 SQL 检查会拦截）"""
        response = "这个查询无法用 SQL 实现，因为 MERGE 语句不被支持。"
        assert extract_sql_from_qwen(response) == response

    def test_empty_code_block(self):
        """```sql``` 内为空 → 提取出空字符串（后续空 SQL 检查拦截）"""
        text = "建议如下：\n```sql\n\n```"
        assert extract_sql_from_qwen(text) == ""

    def test_sql_surrounded_by_noise(self):
        """大量无关文本中嵌入有效 SQL 代码块"""
        text = (
            "根据您的需求，我生成了以下 SQL：\n\n"
            "需要注意 GreptimeDB 不支持 approx_percentile，改用 approx_percentile_cont。\n\n"
            "```sql\n"
            "SELECT approx_percentile_cont(0.95, greptime_value) FROM t\n"
            "```\n\n"
            "这个查询会返回 P95 分位数。"
        )
        result = extract_sql_from_qwen(text)
        assert "approx_percentile_cont" in result
        assert "0.95" in result

    def test_whitespace_only_sql_block(self):
        """代码块内只有空白 → 提取出空白字符串"""
        text = "```sql\n   \n```"
        result = extract_sql_from_qwen(text)
        assert result.strip() == ""


# ── read_text / save_raw_text ──────────────────────────────────

class TestReadWriteText:

    def test_roundtrip(self, tmp_path):
        filepath = tmp_path / "test.txt"
        content = "第一行\n第二行\n第三行"
        save_raw_text(str(filepath), content)
        result = read_text(str(filepath))
        assert result == ["第一行", "第二行", "第三行"]

    def test_read_strips_whitespace(self, tmp_path):
        filepath = tmp_path / "test.txt"
        filepath.write_text("  hello  \n  world  \n", encoding="utf-8")
        result = read_text(str(filepath))
        assert result == ["hello", "world"]


# ── read_json_file / write_json_to_file ────────────────────────

class TestJsonFileOps:

    def test_write_and_read_json_array(self, tmp_path):
        filepath = tmp_path / "data.json"
        data = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        write_json_to_file(str(filepath), data)
        result = read_json_file(str(filepath))
        assert result == data

    def test_write_and_read_jsonl(self, tmp_path):
        filepath = tmp_path / "data.jsonl"
        data = [{"id": 1}, {"id": 2}, {"id": 3}]
        write_json_to_file(str(filepath), data, is_json_line=True)
        result = read_json_file(str(filepath))
        assert len(result) == 3
        assert result[0]["id"] == 1

    def test_read_nonexistent_returns_none(self):
        assert read_json_file("/nonexistent/path.json") is None

    def test_read_json_with_filter(self, tmp_path):
        filepath = tmp_path / "data.json"
        data = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "a"}]
        write_json_to_file(str(filepath), data)
        result = read_json_file(str(filepath), filter_func=lambda x: x["v"] == "a")
        assert len(result) == 2

    def test_write_creates_parent_dirs(self, tmp_path):
        filepath = tmp_path / "sub" / "dir" / "data.json"
        write_json_to_file(str(filepath), [1, 2, 3])
        assert filepath.exists()


# ── save_as_csv ────────────────────────────────────────────────

class TestSaveAsCsv:

    def test_save_and_verify(self, tmp_path):
        filepath = tmp_path / "out.csv"
        data = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        save_as_csv(str(filepath), data)
        import pandas as pd
        df = pd.read_csv(filepath)
        assert list(df.columns) == ["a", "b"]
        assert len(df) == 2


# ── valid_path ─────────────────────────────────────────────────

class TestValidPath:

    def test_creates_missing_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "c" / "file.txt"
        valid_path(str(target))
        assert (tmp_path / "a" / "b" / "c").is_dir()

    def test_existing_dir_no_error(self, tmp_path):
        valid_path(str(tmp_path / "file.txt"))  # tmp_path 已存在
