"""集成测试共享 fixtures

所有集成测试需要 MCP Server 在 localhost:8000 运行。
如果 Server 不可达，测试自动 skip。
"""

import asyncio
import pytest

from tests.conftest import MCP_SERVER_URL


def _server_reachable(url: str) -> bool:
    """检测 MCP Server 是否可达"""
    import requests
    try:
        resp = requests.post(
            url,
            json={"jsonrpc": "2.0", "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "pytest-probe", "version": "0.1"}},
                   "id": 0},
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            timeout=5,
        )
        return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def skip_if_no_server():
    """如果 MCP Server 不可达，跳过整个集成测试模块"""
    if not _server_reachable(MCP_SERVER_URL):
        pytest.skip(
            f"MCP Server 不可达 ({MCP_SERVER_URL})，跳过集成测试。"
            "请先启动 Server: python -m xiyan_mcp_server",
            allow_module_level=True,
        )


@pytest.fixture
def fastmcp_client_class():
    """返回 FastMCP Client 类，避免在顶层 import 时强制依赖"""
    from fastmcp.client import Client
    return Client
