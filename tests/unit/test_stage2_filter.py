"""stage2_filter.py 单元测试（P2-GAP-14）"""

import pytest
from unittest.mock import MagicMock

from xiyan_mcp_server.utils.stage2_filter import Stage2Filter, Stage2FilterError


def _make_stage2_filter():
    f = Stage2Filter.__new__(Stage2Filter)
    return f


class TestBuildCandidatesBlock:
    """_build_candidates_block"""

    def test_table_name_only(self):
        f = _make_stage2_filter()
        candidates = [{"table_name": "t1"}]
        result = f._build_candidates_block(candidates)
        assert "1. t1" in result

    def test_with_embedding_content(self):
        f = _make_stage2_filter()
        candidates = [{"table_name": "t1", "embedding_content": "CPU 使用率表"}]
        result = f._build_candidates_block(candidates)
        assert "t1" in result
        assert "CPU 使用率表" in result

    def test_with_friendly_name_and_description(self):
        f = _make_stage2_filter()
        candidates = [
            {"table_name": "t1", "friendly_name": "CPU", "description": "A" * 300}
        ]
        result = f._build_candidates_block(candidates)
        assert "CPU" in result
        assert "..." in result

    def test_multiple_candidates(self):
        f = _make_stage2_filter()
        candidates = [{"table_name": f"t{i}"} for i in range(3)]
        result = f._build_candidates_block(candidates)
        assert "1. t0" in result
        assert "2. t1" in result
        assert "3. t2" in result


class TestParseTableNames:
    """_parse_table_names"""

    def test_plain_json_array(self):
        f = _make_stage2_filter()
        result = f._parse_table_names('["t1", "t2", "t3"]')
        assert result == ["t1", "t2", "t3"]

    def test_markdown_wrapped(self):
        f = _make_stage2_filter()
        result = f._parse_table_names('```json\n["t1", "t2"]\n```')
        assert result == ["t1", "t2"]

    def test_plain_list_string(self):
        f = _make_stage2_filter()
        result = f._parse_table_names('["t1", "t2"]')
        assert result == ["t1", "t2"]

    def test_think_tag_stripped(self):
        f = _make_stage2_filter()
        raw = "<think>分析</think>\n" + '["t1", "t2"]'
        result = f._parse_table_names(raw)
        assert result == ["t1", "t2"]

    def test_regex_fallback(self):
        f = _make_stage2_filter()
        result = f._parse_table_names('some text ["t1", "t2"] more text')
        assert result == ["t1", "t2"]

    def test_empty_raises(self):
        f = _make_stage2_filter()
        with pytest.raises(Stage2FilterError, match="输出为空"):
            f._parse_table_names("")

    def test_invalid_json_raises(self):
        f = _make_stage2_filter()
        with pytest.raises(Stage2FilterError, match="未能从模型输出中提取表名数组"):
            f._parse_table_names("not json at all")

    def test_non_list_json_raises(self):
        f = _make_stage2_filter()
        with pytest.raises(Stage2FilterError, match="未能从模型输出中提取表名数组"):
            f._parse_table_names('{"key": "value"}')
