"""XiYan MCP Server — 生产级测试框架

目录约定：
  unit/        纯函数 / 工具类单元测试，无外部依赖，用 mock 隔离
  integration/ 需要运行中的 MCP Server（localhost:8000）的集成测试
  e2e/         端到端批量查询、长时间运行的场景测试

运行方式：
  pytest                          # 仅运行单元测试（默认）
  pytest -m integration           # 运行集成测试（需要 MCP Server）
  pytest -m e2e                   # 运行端到端测试
  pytest -m ""                    # 运行全部测试（含 unit/integration/e2e）
"""

import os
import sys
from pathlib import Path

import pytest

# 确保 src 在 sys.path 中，以便直接 import xiyan_mcp_server 包
SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


# ── pytest 标记注册 ────────────────────────────────────────────

def pytest_configure(config):
    """注册自定义标记，避免 pytest 警告"""
    config.addinivalue_line("markers", "integration: 需要运行中的 MCP Server")
    config.addinivalue_line("markers", "e2e: 端到端测试，耗时较长")


# ── 公共 Fixtures ──────────────────────────────────────────────

@pytest.fixture
def tmp_output_dir(tmp_path):
    """提供临时输出目录，测试结束后自动清理"""
    output_dir = tmp_path / "test_output"
    output_dir.mkdir()
    return output_dir


@pytest.fixture
def sample_schema_text():
    """示例 schema 文本，用于 query_tracker / schema_retriever 测试"""
    return """\
# Table: abortspanbytes
[
  {"name": "id", "type": "INT", "comment": "主键"},
  {"name": "key", "type": "VARCHAR", "comment": "键"},
  {"name": "value", "type": "DOUBLE", "comment": "字节数"}
]

# Table: sys_cpu_usage
[
  {"name": "HOST_IP", "type": "VARCHAR", "comment": "主机IP"},
  {"name": "greptime_timestamp", "type": "TIMESTAMP", "comment": "时间戳"},
  {"name": "greptime_value", "type": "DOUBLE", "comment": "CPU使用率"}
]

# Table: gc_count
[
  {"name": "id", "type": "INT", "comment": "主键"},
  {"name": "count", "type": "INT", "comment": "GC次数"}
]
"""


# ── MCP Server 连接配置 ────────────────────────────────────────

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000/mcp")


@pytest.fixture
def mcp_server_url():
    """MCP Server 地址，可通过环境变量覆盖"""
    return MCP_SERVER_URL
