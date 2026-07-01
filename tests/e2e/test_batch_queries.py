"""端到端批量查询测试

从 queries.jsonl 加载查询，逐条通过 MCP Server 执行 get_data，
记录成功/失败/耗时，验证批量查询流程的完整性。

标记为 e2e，默认不运行；用 pytest -m e2e 显式触发。
"""

import asyncio
import json
import os
import time
import pytest
from pathlib import Path

from tests.conftest import MCP_SERVER_URL

pytestmark = pytest.mark.e2e

# ── 配置 ───────────────────────────────────────────────────────

DEFAULT_INPUT = Path(__file__).parent.parent.parent / "queries.jsonl"
INPUT_FILE = os.environ.get("E2E_QUERIES_FILE", str(DEFAULT_INPUT))
MAX_QUERIES = int(os.environ.get("E2E_MAX_QUERIES", "10"))
DELAY_BETWEEN = int(os.environ.get("E2E_DELAY", "3"))
SINGLE_TIMEOUT = int(os.environ.get("E2E_TIMEOUT", "180"))


async def _run_one(query: str, timeout: int) -> dict:
    from fastmcp.client import Client
    t0 = time.time()
    try:
        async with Client(MCP_SERVER_URL) as client:
            result = await asyncio.wait_for(
                client.call_tool("get_data", {"query": query, "format": "markdown"}),
                timeout=timeout,
            )
            elapsed = time.time() - t0
            text = result.content[0].text if result.content else ""
            success = not (text.startswith("错误") or text.startswith("Error"))
            return {"success": success, "elapsed_s": round(elapsed, 1), "preview": text[:120]}
    except asyncio.TimeoutError:
        return {"success": False, "elapsed_s": timeout, "preview": "超时"}
    except Exception as e:
        return {"success": False, "elapsed_s": round(time.time() - t0, 1), "preview": f"异常: {e}"}


def _load_queries(limit: int) -> list:
    path = Path(INPUT_FILE)
    if not path.exists():
        pytest.skip(f"查询文件不存在: {path}")
    queries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            queries.append(json.loads(line))
            if len(queries) >= limit:
                break
    return queries


class TestBatchQueries:

    def test_batch_execution(self):
        """批量执行查询，验证成功率 > 0"""
        queries = _load_queries(MAX_QUERIES)
        assert queries, "未加载到任何查询"

        results = []
        for i, q in enumerate(queries):
            r = asyncio.get_event_loop().run_until_complete(
                _run_one(q["query"], SINGLE_TIMEOUT)
            )
            results.append(r)
            if i < len(queries) - 1:
                time.sleep(DELAY_BETWEEN)

        ok = sum(1 for r in results if r["success"])
        total = len(results)
        assert ok > 0, f"全部 {total} 条查询均失败"
        # 打印摘要（pytest -s 时可见）
        print(f"\n批量结果: {ok}/{total} 成功")
