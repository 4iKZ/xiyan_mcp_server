"""get_data 工具集成测试

测试 get_data 工具在 markdown / json / csv 三种格式下的端到端行为。
需要 MCP Server 运行中；不可达时自动 skip。
"""

import asyncio
import pytest

from tests.conftest import MCP_SERVER_URL

pytestmark = pytest.mark.integration

TEST_QUERY = "查询abortspanbytes表格的前5条数据"


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


async def _call_get_data(format_type: str) -> str:
    from fastmcp.client import Client
    async with Client(MCP_SERVER_URL) as client:
        result = await asyncio.wait_for(
            client.call_tool("get_data", {"query": TEST_QUERY, "format": format_type}),
            timeout=120,
        )
        assert not result.is_error, f"get_data 返回错误: {result.content}"
        text = "".join(item.text for item in result.content if hasattr(item, "text"))
        assert text, "返回内容为空"
        return text


class TestGetDataFormats:

    def test_markdown_format(self):
        text = asyncio.get_event_loop().run_until_complete(_call_get_data("markdown"))
        # Markdown 表格至少包含 | 分隔符
        assert "|" in text

    def test_json_format(self):
        text = asyncio.get_event_loop().run_until_complete(_call_get_data("json"))
        # JSON 格式应包含花括号或方括号
        assert "{" in text or "[" in text

    def test_csv_format(self):
        text = asyncio.get_event_loop().run_until_complete(_call_get_data("csv"))
        # CSV 格式应包含逗号
        assert "," in text
