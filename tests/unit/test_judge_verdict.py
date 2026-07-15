"""judge.py _parse_verdict / _format_preview 单元测试（P0-GAP-13）"""

import pytest
from unittest.mock import MagicMock

from xiyan_mcp_server.utils.judge import JudgeModel, JudgeError


class TestFormatPreview:
    """_format_preview 边界条件"""

    def _make(self):
        mock_retriever = MagicMock()
        cfg = {
            "model_name": "test-model",
            "api_url": "http://localhost:8080/v1",
            "api_key": "EMPTY",
        }
        return JudgeModel(cfg, retriever=mock_retriever)

    def test_none_returns_placeholder(self):
        assert self._make()._format_preview(None) == "（无数据）"

    def test_empty_string_returns_empty_placeholder(self):
        assert self._make()._format_preview("") == "（空）"

    def test_plain_string_truncated(self):
        out = self._make()._format_preview("x" * 600)
        assert len(out) == 500

    def test_list_with_fields(self):
        preview = [[1, "a"], [2, "b"]]
        out = self._make()._format_preview(preview, fields=["id", "name"])
        assert "【列】 id | name" in out
        assert "id=1 | name=a" in out

    def test_list_without_fields(self):
        out = self._make()._format_preview([["a", 1]])
        assert "a | 1" in out

    def test_nested_list_rows(self):
        out = self._make()._format_preview([["x", "y"]])
        assert "x | y" in out

    def test_mixed_row_types(self):
        out = self._make()._format_preview([42, "hello"])
        assert "42" in out
        assert "hello" in out


class TestParseVerdict:
    """_parse_verdict JSON 提取"""

    def _make(self):
        mock_retriever = MagicMock()
        cfg = {
            "model_name": "test-model",
            "api_url": "http://localhost:8080/v1",
            "api_key": "EMPTY",
        }
        return JudgeModel(cfg, retriever=mock_retriever)

    def test_plain_json(self):
        result = self._make()._parse_verdict(
            '{"correct": true, "category": "correct", "reason": "ok"}'
        )
        assert result["correct"] is True
        assert result["category"] == "correct"

    def test_think_tag_stripped(self):
        raw = (
            "<think>分析过程</think>\n"
            '{"correct": false, "category": "syntax", "reason": "error"}'
        )
        result = self._make()._parse_verdict(raw)
        assert result["correct"] is False
        assert result["category"] == "syntax"

    def test_markdown_json_block(self):
        raw = '```json\n{"correct": true, "category": "correct", "reason": "ok"}\n```'
        result = self._make()._parse_verdict(raw)
        assert result["correct"] is True

    def test_cot_last_json_wins(self):
        raw = (
            '{"correct": false, "category": "other", "reason": "first"}\n'
            "分析文本\n"
            '{"correct": true, "category": "correct", "reason": "final"}'
        )
        result = self._make()._parse_verdict(raw)
        assert result["correct"] is True
        assert result["category"] == "correct"

    def test_non_nested_fallback(self):
        raw = 'text before {"correct": true, "category": "correct", "reason": "ok"} text after'
        result = self._make()._parse_verdict(raw)
        assert result["correct"] is True

    def test_empty_raises(self):
        with pytest.raises(JudgeError, match="输出为空"):
            self._make()._parse_verdict("")

    def test_whitespace_only_raises(self):
        with pytest.raises(JudgeError, match="无法从 judge 输出中提取 JSON"):
            self._make()._parse_verdict("   ")

    def test_invalid_json_raises(self):
        with pytest.raises(JudgeError, match="无法从 judge 输出中提取 JSON"):
            self._make()._parse_verdict("not json at all")

    def test_string_bool_coercion(self):
        result = self._make()._parse_verdict(
            '{"correct": "true", "category": "correct", "reason": "ok"}'
        )
        assert result["correct"] is True

    def test_conclusion_unwrap(self):
        raw = '{"analysis": {}, "conclusion": {"correct": true, "category": "correct", "reason": "ok"}}'
        result = self._make()._parse_verdict(raw)
        assert result["correct"] is True
        assert result["category"] == "correct"

    def test_unknown_category_fallback(self):
        result = self._make()._parse_verdict(
            '{"correct": true, "category": "unknown_cat", "reason": "ok"}'
        )
        # unknown_cat 归为 other，但 correct=True + category=other 被强制改为 correct
        assert result["category"] == "correct"

    def test_incomplete_semantics_with_correct_forces_false(self):
        result = self._make()._parse_verdict(
            '{"correct": true, "category": "incomplete_semantics", "reason": "ok"}'
        )
        assert result["correct"] is False
        assert result["category"] == "incomplete_semantics"

    def test_infrastructure_category_with_correct_preserved(self):
        result = self._make()._parse_verdict(
            '{"correct": true, "category": "infrastructure_timeout", "reason": "ok"}'
        )
        assert result["correct"] is True
        assert result["category"] == "infrastructure_timeout"
