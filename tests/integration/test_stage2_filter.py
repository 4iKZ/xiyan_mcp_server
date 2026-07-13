"""Stage 2 筛选器集成测试

验证：
    1. config.yml 里的 stage2 配置能被正确读取
    2. Stage2Filter 能正确初始化
    3. 模型调用返回结构化结果
    4. 解析出的表名在候选中存在

需要 MCP Server 运行中（Stage2Filter 调用 vLLM endpoint）；
不可达时自动 skip。

原脚本: scripts/smoke_test_stage2.py
"""

import pytest
import yaml
from pathlib import Path

pytestmark = pytest.mark.integration

CONFIG_PATH = Path(__file__).parent.parent.parent / "src" / "xiyan_mcp_server" / "config.yml"

QUERY = "CPU 使用率最高的 5 个节点"
CANDIDATES = [
    {
        "table_name": "cockroach_metrics.sys_cpu_usage",
        "friendly_name": "系统CPU使用率",
        "description": "记录每个节点 CPU 使用率的时序数据，包含 user/system/idle 占比",
    },
    {
        "table_name": "cockroach_metrics.sql_statements",
        "friendly_name": "SQL 语句执行记录",
        "description": "SQL 语句执行历史与耗时",
    },
    {
        "table_name": "cockroach_metrics.node_status",
        "friendly_name": "节点状态",
        "description": "节点心跳、版本与健康信息",
    },
    {
        "table_name": "cockroach_metrics.disk_io",
        "friendly_name": "磁盘 IO",
        "description": "磁盘读写吞吐统计",
    },
    {
        "table_name": "cockroach_metrics.memory_usage",
        "friendly_name": "内存使用",
        "description": "内存使用情况",
    },
]


@pytest.fixture(scope="module")
def stage2_config():
    """从 config.yml 读取 stage2 配置"""
    if not CONFIG_PATH.exists():
        pytest.skip(f"配置文件不存在: {CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    stage2_cfg = cfg.get("schema_filter", {}).get("stage2", {}) or {}
    if not stage2_cfg:
        pytest.skip("config.yml 中没有 schema_filter.stage2 配置")
    return stage2_cfg


@pytest.fixture(scope="module")
def stage2_filter(stage2_config):
    """初始化 Stage2Filter"""
    from xiyan_mcp_server.utils.stage2_filter import Stage2Filter
    return Stage2Filter(stage2_config)


class TestStage2Filter:

    def test_config_readable(self, stage2_config):
        """配置能被正确读取"""
        assert "model_name" in stage2_config
        assert "api_url" in stage2_config

    def test_filter_initializes(self, stage2_filter):
        """Stage2Filter 能正确初始化"""
        assert stage2_filter is not None

    async def test_filter_returns_results(self, stage2_filter):
        """模型调用返回非空结果"""
        kept = await stage2_filter.filter(query=QUERY, candidates=CANDIDATES, top_m=2)
        assert len(kept) > 0, "精筛结果为空"

    async def test_result_table_names_valid(self, stage2_filter):
        """返回的表名在候选中存在"""
        kept = await stage2_filter.filter(query=QUERY, candidates=CANDIDATES, top_m=2)
        candidate_names = {c["table_name"] for c in CANDIDATES}
        for item in kept:
            assert item["table_name"] in candidate_names, (
                f"返回了不在候选中的表: {item['table_name']}"
            )
