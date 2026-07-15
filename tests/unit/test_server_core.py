"""server.py 核心函数单元测试（P0-GAP-08）"""

import os
import pytest
from unittest.mock import patch, MagicMock
import yaml

from xiyan_mcp_server.server import (
    _expand_env_vars,
    get_yml_config,
    validate_config,
    _inject_limit,
    format_result,
)


class TestExpandEnvVars:
    """环境变量递归展开"""

    def test_plain_string_no_match(self):
        assert _expand_env_vars("hello world") == "hello world"

    def test_simple_var(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert _expand_env_vars("${FOO}") == "bar"

    def test_var_with_default_when_missing(self):
        assert _expand_env_vars("${FOO:-default}") == "default"

    def test_var_with_default_when_set(self, monkeypatch):
        monkeypatch.setenv("FOO", "real")
        assert _expand_env_vars("${FOO:-default}") == "real"

    def test_nested_dict(self, monkeypatch):
        monkeypatch.setenv("HOST", "0.0.0.0")
        obj = {"db": {"host": "${HOST}", "port": 5432}}
        result = _expand_env_vars(obj)
        assert result == {"db": {"host": "0.0.0.0", "port": 5432}}

    def test_nested_list(self, monkeypatch):
        monkeypatch.setenv("URL", "http://example.com")
        assert _expand_env_vars(["${URL}", "static"]) == [
            "http://example.com",
            "static",
        ]

    def test_non_string_leaf(self):
        assert _expand_env_vars(42) == 42
        assert _expand_env_vars(None) is None


class TestGetYmlConfig:
    """配置文件读取 + 环境变量展开"""

    def test_returns_dict(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yml"
        cfg_file.write_text("model:\n  name: test-model\n")
        monkeypatch.setenv("YML", str(cfg_file))
        result = get_yml_config()
        assert isinstance(result, dict)
        assert result["model"]["name"] == "test-model"

    def test_file_not_found_raises(self, monkeypatch):
        monkeypatch.setenv("YML", "/nonexistent/path/config.yml")
        with pytest.raises(FileNotFoundError):
            get_yml_config()

    def test_invalid_yaml_raises(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "bad.yml"
        cfg_file.write_text("key: [unclosed")
        monkeypatch.setenv("YML", str(cfg_file))
        with pytest.raises(yaml.YAMLError):
            get_yml_config()


class TestValidateConfig:
    """配置校验"""

    def test_missing_model_section_raises(self):
        with pytest.raises(ValueError, match="缺少必需的 section"):
            validate_config({"database": {"dialect": "sqlite"}})

    def test_model_missing_name_raises(self):
        with pytest.raises(ValueError, match="缺少必需字段"):
            validate_config(
                {"model": {"key": "k", "url": "u"}, "database": {"dialect": "sqlite"}}
            )

    def test_sqlite_only_needs_dialect(self):
        validate_config(
            {
                "model": {"name": "m", "key": "k", "url": "u"},
                "database": {"dialect": "sqlite"},
            }
        )

    def test_mysql_missing_db_fields_raises(self):
        with pytest.raises(ValueError, match="缺少必需字段"):
            validate_config(
                {
                    "model": {"name": "m", "key": "k", "url": "u"},
                    "database": {"dialect": "mysql", "host": "h"},
                }
            )


class TestInjectLimit:
    """LIMIT 自动注入"""

    def test_already_has_limit(self):
        sql = "SELECT * FROM t LIMIT 10"
        result, injected = _inject_limit(sql)
        assert result == sql
        assert injected is False

    def test_no_limit_select_adds_default(self):
        sql = "SELECT * FROM t"
        result, injected = _inject_limit(sql)
        assert result == "SELECT * FROM t LIMIT 500"
        assert injected is True

    def test_aggregate_query_adds_1000(self):
        sql = "SELECT COUNT(*) FROM t GROUP BY x"
        result, injected = _inject_limit(sql)
        assert result == "SELECT COUNT(*) FROM t GROUP BY x LIMIT 1000"
        assert injected is True

    def test_empty_sql_returns_unchanged(self):
        sql = ""
        assert _inject_limit(sql) == (sql, False)

    def test_sql_with_semicolon_stripped_before_limit(self):
        sql = "SELECT * FROM t;"
        result, injected = _inject_limit(sql)
        assert result == "SELECT * FROM t LIMIT 500"
        assert injected is True

    def test_subquery_limit_not_counted(self):
        sql = "SELECT * FROM (SELECT * FROM t LIMIT 10) sub"
        result, injected = _inject_limit(sql)
        assert "LIMIT 500" in result
        assert injected is True

    def test_case_insensitive_limit(self):
        sql = "select * from t limit 5"
        result, injected = _inject_limit(sql)
        assert result == sql
        assert injected is False


class TestFormatResult:
    """查询结果格式化"""

    def test_markdown_format(self):
        result = format_result(
            {"truncated_results": [["a", 1], ["b", 2]], "fields": ["name", "val"]},
            "markdown",
        )
        assert "| name | val |" in result
        assert "| a | 1 |" in result

    def test_json_format(self):
        result = format_result(
            {"truncated_results": [["a", 1]], "fields": ["name", "val"]},
            "json",
        )
        assert '"name": "a"' in result
        assert '"val": 1' in result

    def test_csv_format(self):
        result = format_result(
            {"truncated_results": [["a", 1]], "fields": ["name", "val"]},
            "csv",
        )
        assert result.startswith("name,val")
        assert "a,1" in result

    def test_error_result_string(self):
        result = format_result({"truncated_results": "timeout", "fields": []})
        assert result.startswith("Error:")

    def test_empty_fields_returns_dict_string(self):
        result = format_result({"truncated_results": [], "fields": []})
        assert result == str({"truncated_results": [], "fields": []})

    def test_non_dict_input(self):
        result = format_result("not a dict")  # type: ignore
        assert "Unexpected result type" in result
