#!/usr/bin/env python3
"""
XiYan MCP Server — 自然语言 HDFS 路径测试

测试理念：
  只给一句自然语言查询，不预设任何路径参数。由服务器 LLM 自动解析查询中的
  HDFS 存储意图，测试通过校验返回路径的**结构特征**来判断解析是否正确。

测试范围：
  - get_data: 自然语言查询，覆盖 markdown / json / csv 三种输出格式
  - query_and_upload_to_hdfs: 纯自然语言描述 HDFS 存储路径
    1. 默认路径 — 不指定路径 → 时间戳目录 + 时间戳文件名
    2. 纯文件名 — 描述文件名 → 时间戳目录 + 自定义文件名
    3. 目录路径 — 描述目录 → 自定义目录 + 时间戳文件名
    4. 完整路径 — 描述完整路径 → 自定义目录 + 自定义文件名
  - session_id 向后兼容

使用方式:
    python test_mcp_nl_path.py
"""

import asyncio
import re
import sys
from datetime import datetime

from fastmcp.client import Client

SERVER_URL = "http://localhost:8000/mcp"
CALL_TIMEOUT = 300  # 单次调用超时（秒），含 LLM + DB + HDFS 上传

# ── 测试数据集：只给自然语言，不预设任何路径参数 ──────────────

TEST_GET_DATA_QUERY = "查询abortspanbytes表格的前5条数据"

NL_HDFS_TEST_CASES = [
    # ═══ 模式1: 默认路径 — 不提及任何路径信息 ═══
    {
        "label": "默认-1",
        "query": "查询abortspanbytes表格的前30条数据，将结果上传到HDFS",
        "verify": "default",
    },
    {
        "label": "默认-2",
        "query": "帮我查abortspanbytes表的前30条记录并上传HDFS",
        "verify": "default",
    },

    # ═══ 模式2: 纯文件名 — 描述文件名，不指定目录 ═══
    {
        "label": "文件名-1: 命名为",
        "query": "查询abortspanbytes表格的前30条数据，将文件命名为my_custom_report上传到HDFS",
        "verify": "filename",
    },
    {
        "label": "文件名-2: 文件名为",
        "query": "查询abortspanbytes表格的前30条数据，文件名为special_export并上传到HDFS",
        "verify": "filename",
    },
    {
        "label": "文件名-3: 保存为",
        "query": "查询abortspanbytes表格的前30条数据，保存为batch_result文件上传HDFS",
        "verify": "filename",
    },

    # ═══ 模式3: 目录路径 — 描述目录，不指定文件名 ═══
    {
        "label": "目录-1: xxx目录下",
        "query": "查询abortspanbytes表格的前30条数据，上传到HDFS的test_batch目录下",
        "verify": "directory",
    },
    {
        "label": "目录-2: xxx文件夹",
        "query": "查询abortspanbytes表格的前30条数据，保存在project_a文件夹下并上传HDFS",
        "verify": "directory",
    },
    {
        "label": "目录-3: xxx路径",
        "query": "查询abortspanbytes表格的前30条数据，存储到HDFS的data_export路径下",
        "verify": "directory",
    },

    # ═══ 模式4: 完整路径 — 同时描述目录和文件名 ═══
    {
        "label": "完整路径-1: 保存到dir/file",
        "query": "查询abortspanbytes表格的前30条数据，保存到project_a/daily_report并上传HDFS",
        "verify": "fullpath",
    },
    {
        "label": "完整路径-2: 存储为dir/file",
        "query": "查询abortspanbytes表格的前30条数据，存储为exports/weekly_summary后上传",
        "verify": "fullpath",
    },
    {
        "label": "完整路径-3: 路径是dir/file",
        "query": "查询abortspanbytes表格的前30条数据，HDFS路径设为analysis/monthly_stats并上传",
        "verify": "fullpath",
    },
    {
        "label": "完整路径-4: 中文复杂描述",
        "query": "帮我查abortspanbytes表前30条数据，导出到HDFS，放在reports/2025/summary_report这个路径下",
        "verify": "fullpath",
    },
]


# ── 输出辅助 ──────────────────────────────────────────────────

def hr(title: str, char: str = "=", width: int = 64) -> None:
    print(f"\n{char * width}")
    print(f"  {title}")
    print(f"{char * width}")


def print_result(result, max_len: int = 4000) -> None:
    if result.is_error:
        print("[服务端错误]")
        for item in result.content:
            print(f"  {item.text}")
        return
    for item in result.content:
        if hasattr(item, "text"):
            text = item.text
            if len(text) > max_len:
                print(text[:max_len])
                print(f"\n... (输出被截断，共 {len(text)} 字符)")
            else:
                print(text)


def status(ok: bool) -> str:
    return "✓ 通过" if ok else "✗ 失败"


# ── 验证：通过路径结构特征判断解析是否正确 ───────────────────

# 默认自动生成的路径特征
TIMESTAMP_DIR_PATTERN = re.compile(
    r'/user_custom_data/\d{8}_\d{6}_batch_\d{3}/parquet/'
)
TIMESTAMP_FILE_PATTERN = re.compile(
    r'/\d{8}_\d{6}\.parquet$'
)


def parse_hdfs_path(text: str) -> dict | None:
    """从响应文本中解析 HDFS 路径，拆分为目录部分和文件名部分"""
    m = re.search(r'(/[\w\-_/]+\.parquet)', text)
    if not m:
        return None
    full_path = m.group(1)
    # 分离目录和文件名: /user_custom_data/.../parquet/filename.parquet
    dir_part, file_part = full_path.rsplit("/", 1)
    dir_part += "/"
    return {
        "full": full_path,
        "dir": dir_part,
        "file": file_part,
    }


def verify_hdfs_response(text: str, verify_mode: str) -> bool:
    """根据路径结构特征验证服务器是否正确理解了自然语言意图

    不依赖任何预设的具体路径值，只根据路径结构判断：
    - default:  时间戳目录 + 时间戳文件名（全自动）
    - filename: 时间戳目录 + 非时间戳文件名（文件名是用户取的）
    - directory: 非时间戳目录 + 时间戳文件名（目录是用户取的）
    - fullpath:  非时间戳目录 + 非时间戳文件名（都是用户取的）
    """
    parsed = parse_hdfs_path(text)
    if not parsed:
        print(f"    ⚠ 未在响应中找到有效的 parquet 路径")
        return False

    print(f"    返回路径: {parsed['full']}")

    dir_is_auto = bool(TIMESTAMP_DIR_PATTERN.search(parsed["dir"]))
    file_is_auto = bool(TIMESTAMP_FILE_PATTERN.search(parsed["full"]))

    print(f"    目录: {'自动时间戳' if dir_is_auto else '自定义'}  |  "
          f"文件名: {'自动时间戳' if file_is_auto else '自定义'}")

    checks = {
        "default":   (True,  True),   # 目录自动 + 文件名自动
        "filename":  (True,  False),  # 目录自动 + 文件名自定义
        "directory": (False, True),   # 目录自定义 + 文件名自动
        "fullpath":  (False, False),  # 目录自定义 + 文件名自定义
    }

    expected_dir, expected_file = checks[verify_mode]
    ok = (dir_is_auto == expected_dir) and (file_is_auto == expected_file)

    if ok:
        print(f"    ✓ 路径结构符合「{verify_mode}」模式")
    else:
        expected_dir_str = "自动" if expected_dir else "自定义"
        expected_file_str = "自动" if expected_file else "自定义"
        print(f"    ✗ 期望: 目录={expected_dir_str}, 文件名={expected_file_str}")

    return ok


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
            assert "query_and_upload_to_hdfs" in tool_names, "缺少 query_and_upload_to_hdfs 工具"
            print(f"状态: {status(True)}")
            return True
    except Exception as e:
        print(f"状态: {status(False)} — {e}")
        return False


# ── get_data 测试 ─────────────────────────────────────────────

async def test_get_data(query: str, format_type: str) -> bool:
    hr(f"get_data | format={format_type}")
    print(f"查询: {query[:80]}...")
    try:
        async with Client(SERVER_URL) as client:
            result = await asyncio.wait_for(
                client.call_tool("get_data", {"query": query, "format": format_type}),
                timeout=CALL_TIMEOUT,
            )
            print(f"状态: {status(True)}")
            print_result(result, max_len=2000)
            return True
    except asyncio.TimeoutError:
        print(f"状态: {status(False)} — 超时 ({CALL_TIMEOUT}s)")
        return False
    except Exception as e:
        print(f"状态: {status(False)} — {type(e).__name__}: {e}")
        return False


# ── query_and_upload_to_hdfs 测试（纯自然语言）───────────────

async def test_hdfs_upload_nl(
    query: str,
    label: str,
    verify_mode: str,
    session_id: str = "",
) -> bool:
    """纯自然语言 HDFS 上传测试 — 不传 hdfs_path，完全依赖服务器自动解析"""
    hr(f"HDFS上传 | {label}")
    print(f"查询: {query[:100]}...")
    if session_id:
        print(f"session_id: {session_id}")

    try:
        async with Client(SERVER_URL) as client:
            args = {"query": query}
            if session_id:
                args["session_id"] = session_id

            result = await asyncio.wait_for(
                client.call_tool("query_and_upload_to_hdfs", args),
                timeout=CALL_TIMEOUT,
            )

            text = ""
            for item in result.content:
                if hasattr(item, "text"):
                    text += item.text

            has_error = "错误" in text or "失败" in text
            if has_error:
                print(f"状态: {status(False)}")
                print(f"  响应: {text[:500]}")
                return False

            print(f"状态: {status(True)}")
            print_result(result, max_len=500)

            if not verify_hdfs_response(text, verify_mode):
                return False
            return True

    except asyncio.TimeoutError:
        print(f"状态: {status(False)} — 超时 ({CALL_TIMEOUT}s, HDFS 上传可能耗时较长)")
        return False
    except Exception as e:
        print(f"状态: {status(False)} — {type(e).__name__}: {e}")
        return False


# ── 主入口 ────────────────────────────────────────────────────

async def main() -> None:
    hr("XiYan MCP Server - 自然语言 HDFS 路径测试", char="#", width=70)
    print(f"服务地址: {SERVER_URL}")
    print(f"测试时间: {datetime.now().isoformat()}")
    print()
    print("测试理念：只给一句自然语言，不预设任何路径参数。")
    print("通过校验返回路径的结构特征来判断 LLM 解析是否正确。")
    print()
    print("验证规则（结构特征匹配）：")
    print("  默认路径  → 目录=自动时间戳  文件名=自动时间戳")
    print("  纯文件名  → 目录=自动时间戳  文件名=自定义")
    print("  目录路径  → 目录=自定义      文件名=自动时间戳")
    print("  完整路径  → 目录=自定义      文件名=自定义")

    ok = await test_connection()
    if not ok:
        print("\n连接失败，请确认 MCP Server 已启动。")
        sys.exit(1)

    results = []

    # ── 第 1 步：get_data 三种格式 ──
    print()
    hr("1. get_data 测试（三种输出格式）", char="-")
    for fmt in ("markdown", "json", "csv"):
        ok = await test_get_data(TEST_GET_DATA_QUERY, format_type=fmt)
        results.append((f"get_data ({fmt})", ok))

    # ── 第 2 步：自然语言 HDFS 路径测试 ──
    print()
    hr("2. 自然语言 HDFS 路径测试", char="-")

    mode_names = {
        "default": "默认路径",
        "filename": "纯文件名",
        "directory": "目录路径",
        "fullpath": "完整路径",
    }
    current_mode = None
    for tc in NL_HDFS_TEST_CASES:
        mode = tc["verify"]
        if mode != current_mode:
            current_mode = mode
            print(f"\n  ── {mode_names.get(mode, mode)} ──")
        ok = await test_hdfs_upload_nl(tc["query"], tc["label"], tc["verify"])
        results.append((tc["label"], ok))

    # ── 第 3 步：session_id 向后兼容 ──
    print()
    hr("3. session_id 向后兼容测试", char="-")
    ok = await test_hdfs_upload_nl(
        query="查询abortspanbytes表格的前30条数据，上传到HDFS",
        label="session_id 向后兼容",
        verify_mode="default",
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
        print(f"  [{'✓' if ok else '✗'}] {name}")
    print()
    if failed > 0:
        print(f"有 {failed} 个测试未通过，请检查日志。")
        sys.exit(1)
    else:
        print("全部测试通过！")


if __name__ == "__main__":
    asyncio.run(main())
