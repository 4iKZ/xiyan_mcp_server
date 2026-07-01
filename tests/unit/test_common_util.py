"""common_util 单元测试"""

import re
from xiyan_mcp_server.utils.common_util import get_timestamp, extract_llm_messages


class TestGetTimestamp:

    def test_returns_iso_format(self):
        ts = get_timestamp()
        # 格式: YYYY-MM-DDTHH:MM:SS
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts)

    def test_returns_string(self):
        assert isinstance(get_timestamp(), str)


class TestExtractLlmMessages:

    def test_filters_valid_roles(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "tool", "content": "result"},
            {"role": "invalid_role", "content": "should be removed"},
        ]
        result = extract_llm_messages(messages)
        assert len(result) == 4
        assert all(m["role"] in ("system", "assistant", "user", "tool") for m in result)

    def test_empty_list(self):
        assert extract_llm_messages([]) == []

    def test_all_invalid(self):
        messages = [{"role": "unknown", "content": "x"}]
        assert extract_llm_messages(messages) == []
