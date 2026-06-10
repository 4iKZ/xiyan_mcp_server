#!/usr/bin/env python3
"""
XiYan MCP Server 完整功能测试

测试范围：
  - get_data: 自然语言查询，覆盖 markdown / json / csv 三种输出格式
  - query_and_upload_to_hdfs: 查询并上传 HDFS，覆盖全部 4 种 hdfs_path 模式
    1. 默认路径（空 hdfs_path，自动时间戳生成）
    2. 纯文件名（无 /）→ 时间戳目录 + 自定义文件名
    3. 目录路径（以 / 结尾）→ 自定义目录 + 自动时间戳文件名
    4. 完整路径（含 / 不以 / 结尾）→ 自定义目录 + 自定义文件名
  - session_id 向后兼容

使用方式:
    python test_mcp_full.py
"""

import asyncio
import sys
from datetime import datetime

from fastmcp.client import Client

SERVER_URL = "http://localhost:8000/mcp"

# 测试用自然语言查询（请根据实际数据库调整）
TEST_QUERY_SIMPLE = "查询abortspanbytes表格的前5条数据"
TEST_QUERY_HDFS = "查询abortspanbytes表格的前30条数据"

# ── 输出辅助 ──────────────────────────────────────────────────


def hr(title: str, char: str = "=", width: int = 64) -> None:
    print(f"\n{char * width}")
    print(f"  {title}")
    print(f"{char * width}")


def print_result(result) -> None:
    """格式化打印 CallToolResult"""
    if result.is_error:
        print("[服务端错误]")
        for item in result.content:
            print(f"  {item.text}")
        return

    for item in result.content:
        if hasattr(item, "text"):
            text = item.text
            if len(text) > 3000:
                print(text[:3000])
                print(f"\n... (输出被截断，共 {len(text)} 字符)")
            else:
                print(text)


def status(ok: bool) -> str:
    return "✓ 通过" if ok else "✗ 失败"


# ── 连接测试 ──────────────────────────────────────────────────


async def test_connection() -> bool:
    hr("连接测试")
    try:
        async with Client(SERVER_URL) as client:
            print(f"已连接到: {SERVER_URL}")
            tools = await client.list_tools()
            tool_names = [t.name for t in tools]
            print(f"可用工具 ({len(tools)}): {', '.join(tool_names)}")

            assert "get_data" in tool_names, "缺少 get_data 工具"
            assert "query_and_upload_to_hdfs" in tool_names, (
                "缺少 query_and_upload_to_hdfs 工具"
            )
            print(f"状态: {status(True)}")
            return True
    except Exception as e:
        print(f"状态: {status(False)} — {e}")
        return False


# ── get_data 测试 ─────────────────────────────────────────────


async def test_get_data(query: str, format_type: str) -> bool:
    """测试 get_data 工具 — 自然语言 → SQL → 格式化结果"""
    hr(f"get_data | format={format_type}")
    print(f"查询: {query[:80]}...")

    try:
        async with Client(SERVER_URL) as client:
            result = await client.call_tool(
                "get_data",
                {"query": query, "format": format_type},
            )
            print(f"状态: {status(True)}")
            print_result(result)
            return True
    except asyncio.TimeoutError:
        print(f"状态: {status(False)} — 超时")
        return False
    except Exception as e:
        print(f"状态: {status(False)} — {type(e).__name__}: {e}")
        return False


# ── query_and_upload_to_hdfs 测试 ──────────────────────────────


async def test_hdfs_upload(
    query: str,
    label: str,
    session_id: str = "",
    hdfs_path: str = "",
) -> bool:
    """
    测试 query_and_upload_to_hdfs 工具

    Args:
        query: 自然语言查询
        label: 测试用例名称（用于输出）
        session_id: 可选会话 ID（向后兼容）
        hdfs_path: 自定义 HDFS 路径（新功能）
    """
    hr(f"query_and_upload_to_hdfs | {label}")
    print(f"查询: {query[:80]}...")
    if session_id:
        print(f"session_id: {session_id}")
    if hdfs_path:
        print(f"hdfs_path:   {hdfs_path}")

    try:
        async with Client(SERVER_URL) as client:
            args = {"query": query}
            if session_id:
                args["session_id"] = session_id
            if hdfs_path:
                args["hdfs_path"] = hdfs_path

            result = await client.call_tool(
                "query_and_upload_to_hdfs",
                args,
            )
            print(f"状态: {status(True)}")
            print_result(result)
            return True
    except asyncio.TimeoutError:
        print(f"状态: {status(False)} — 超时 (HDFS 上传可能耗时较长)")
        return False
    except Exception as e:
        print(f"状态: {status(False)} — {type(e).__name__}: {e}")
        return False


# ── 主入口 ────────────────────────────────────────────────────


async def main() -> None:
    hr("XiYan MCP Server - 完整功能测试", char="#", width=70)
    print(f"服务地址: {SERVER_URL}")
    print(f"测试时间: {datetime.now().isoformat()}")
    print()
    print("测试 HDFS 路径模式:")
    print("  1. 默认 — 全自动时间戳路径 + 文件名")
    print("  2. 纯文件名 — 时间戳目录 + 用户指定文件名")
    print("  3. 目录/ — 用户指定目录 + 自动时间戳文件名")
    print("  4. 目录/文件名 — 用户指定完整路径")

    # ── 第 0 步：连接测试 ──
    ok = await test_connection()
    if not ok:
        print("\n连接失败，请确认 MCP Server 已启动。")
        sys.exit(1)

    results = []

    # ── 第 1 步：get_data 三种格式 ──
    print()
    hr("1. get_data 测试（三种输出格式）", char="-")

    for fmt in ("markdown", "json", "csv"):
        ok = await test_get_data(TEST_QUERY_SIMPLE, format_type=fmt)
        results.append((f"get_data ({fmt})", ok))

    # ── 第 2 步：query_and_upload_to_hdfs 四种 hdfs_path 模式 ──
    print()
    hr("2. query_and_upload_to_hdfs 测试（四种路径模式）", char="-")

    # 模式 1: 空 → 全自动时间戳生成
    ok = await test_hdfs_upload(
        TEST_QUERY_HDFS,
        label="模式1: 默认路径（空 hdfs_path）",
    )
    results.append(("hdfs: 默认路径", ok))

    # 模式 2: 纯文件名（无 /）→ 时间戳目录 + 自定义文件名
    ok = await test_hdfs_upload(
        TEST_QUERY_HDFS,
        label="模式2: 纯文件名（无 /）",
        hdfs_path="my_custom_report",
    )
    results.append(("hdfs: 纯文件名", ok))

    # 模式 3: 目录/ （以 / 结尾）→ 自定义目录 + 自动时间戳文件名
    ok = await test_hdfs_upload(
        TEST_QUERY_HDFS,
        label="模式3: 目录路径（以 / 结尾）",
        hdfs_path="test_batch/",
    )
    results.append(("hdfs: 目录路径", ok))

    # 模式 4: 目录/文件名（含 / 不以 / 结尾）→ 自定义完整路径
    ok = await test_hdfs_upload(
        TEST_QUERY_HDFS,
        label="模式4: 完整路径（目录+文件名）",
        hdfs_path="project_a/daily_report",
    )
    results.append(("hdfs: 完整路径", ok))

    # ── 第 3 步：session_id 向后兼容测试 ──
    print()
    hr("3. session_id 向后兼容测试", char="-")

    ok = await test_hdfs_upload(
        TEST_QUERY_HDFS,
        label="session_id 向后兼容",
        session_id="backward_compat_test",
    )
    results.append(("hdfs: session_id 兼容", ok))

    # ── 汇总 ──
    hr("测试汇总")
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    total = len(results)

    print(f"  总计: {total}  通过: {passed}  失败: {failed}")
    print()

    for name, ok in results:
        print(f"  [{status(ok)[0]}] {name}")

    print()

    if failed > 0:
        print(f"有 {failed} 个测试未通过，请检查日志。")
        sys.exit(1)
    else:
        print("全部测试通过！")


if __name__ == "__main__":
    asyncio.run(main())
