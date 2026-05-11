#!/usr/bin/env python3
"""
FastMCP 测试文件：测试 XiYan MCP Server 的两个核心工具

- get_data: 自然语言 → SQL → 数据库查询 → 格式化数据表
- query_and_upload_to_hdfs: 自然语言 → SQL → 查询 → Parquet → HDFS 路径

使用 FastMCP Client 连接 MCP Server 进行测试。
"""

import asyncio
import sys
from fastmcp.client import Client

# MCP 服务地址
SERVER_URL = "http://localhost:8000/mcp"

# 测试查询语句
TEST_QUERIES = {
    "simple": "查询abortspanbytes表格的前5条数据",
    "hdfs": "查询abortspanbytes表格的前50条数据",
}


def print_divider(title: str, char: str = "=", width: int = 60) -> None:
    """打印分隔标题"""
    print(f"\n{char * width}")
    print(f"  {title}")
    print(f"{char * width}")


def print_result(result) -> None:
    """格式化打印 CallToolResult"""
    # 检查是否有错误
    if result.is_error:
        print(f"[服务端错误]")
        for item in result.content:
            print(f"  {item.text}")
        return

    # 打印文本内容
    for item in result.content:
        if hasattr(item, "text"):
            text = item.text
            # 如果内容较长，截断显示
            if len(text) > 2000:
                print(text[:2000])
                print(f"\n... (输出被截断，共 {len(text)} 字符)")
            else:
                print(text)


async def test_get_data(
    query: str, format_type: str = "markdown"
) -> bool:
    """
    测试 get_data 工具 —— 自然语言查询返回格式化数据

    Args:
        query: 自然语言查询
        format_type: 输出格式 (markdown / json / csv)

    Returns:
        测试是否通过
    """
    print_divider(f"get_data | format={format_type}")
    print(f"查询: {query[:80]}...")

    try:
        async with Client(SERVER_URL) as client:
            result = await client.call_tool(
                "get_data",
                {"query": query, "format": format_type},
            )
            print("状态: 成功")
            print_result(result)
            return True

    except asyncio.TimeoutError:
        print("状态: 超时")
        return False
    except Exception as e:
        print(f"状态: 异常 — {type(e).__name__}: {e}")
        return False


async def test_query_and_upload_to_hdfs(
    query: str, session_id: str = ""
) -> bool:
    """
    测试 query_and_upload_to_hdfs 工具 —— 自然语言查询并上传到 HDFS

    Args:
        query: 自然语言查询
        session_id: 可选会话 ID

    Returns:
        测试是否通过
    """
    print_divider("query_and_upload_to_hdfs")
    print(f"查询: {query[:80]}...")
    if session_id:
        print(f"Session ID: {session_id}")

    try:
        async with Client(SERVER_URL) as client:
            result = await client.call_tool(
                "query_and_upload_to_hdfs",
                {"query": query, "session_id": session_id},
            )
            print("状态: 成功")
            print_result(result)
            return True

    except asyncio.TimeoutError:
        print("状态: 超时 (HDFS 上传可能需要较长时间)")
        return False
    except Exception as e:
        print(f"状态: 异常 — {type(e).__name__}: {e}")
        return False


async def test_connection() -> bool:
    """测试 MCP Server 连接"""
    print_divider("连接测试")
    try:
        async with Client(SERVER_URL) as client:
            print(f"已连接到: {SERVER_URL}")
            # 列出可用工具验证连接
            tools = await client.list_tools()
            tool_names = [t.name for t in tools]
            print(f"可用工具 ({len(tools)}): {', '.join(tool_names)}")

            # 验证两个核心工具存在
            assert "get_data" in tool_names, "缺少 get_data 工具"
            assert "query_and_upload_to_hdfs" in tool_names, (
                "缺少 query_and_upload_to_hdfs 工具"
            )
            print("状态: 连接测试通过 ✓")
            return True

    except Exception as e:
        print(f"状态: 连接失败 — {e}")
        return False


async def main() -> None:
    """主测试入口"""
    print_divider("XiYan MCP Server - FastMCP 测试", char="#", width=70)
    print(f"服务地址: {SERVER_URL}")
    print(f"测试时间: {__import__('datetime').datetime.now().isoformat()}")

    # 统计
    passed = 0
    failed = 0
    tests = []

    # 1. 连接测试
    ok = await test_connection()
    if not ok:
        print("\n连接失败，终止测试。请确认 MCP Server 已启动。")
        sys.exit(1)

    tests.append(("连接测试", ok))
    if ok:
        passed += 1
    else:
        failed += 1

    # 2. get_data — Markdown 格式
    ok = await test_get_data(TEST_QUERIES["simple"], format_type="markdown")
    tests.append(("get_data (markdown)", ok))
    if ok:
        passed += 1
    else:
        failed += 1

    # 3. get_data — JSON 格式
    ok = await test_get_data(TEST_QUERIES["simple"], format_type="json")
    tests.append(("get_data (json)", ok))
    if ok:
        passed += 1
    else:
        failed += 1

    # 4. get_data — CSV 格式
    ok = await test_get_data(TEST_QUERIES["simple"], format_type="csv")
    tests.append(("get_data (csv)", ok))
    if ok:
        passed += 1
    else:
        failed += 1

    # 5. query_and_upload_to_hdfs
    ok = await test_query_and_upload_to_hdfs(
        TEST_QUERIES["hdfs"], session_id="fastmcp_test"
    )
    tests.append(("query_and_upload_to_hdfs", ok))
    if ok:
        passed += 1
    else:
        failed += 1

    # 汇总
    print_divider("测试汇总")
    total = len(tests)
    print(f"  总计: {total}  通过: {passed}  失败: {failed}")
    print()
    for name, ok in tests:
        mark = "✓" if ok else "✗"
        print(f"  [{mark}] {name}")
    print()

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
