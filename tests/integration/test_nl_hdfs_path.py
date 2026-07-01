"""自然语言 HDFS 路径集成测试

测试理念：只给一句自然语言查询，不预设任何路径参数。
由服务器 LLM 自动解析查询中的 HDFS 存储意图，
通过校验返回路径的结构特征来判断解析是否正确。

需要 MCP Server 运行中；不可达时自动 skip。
"""

import asyncio
import re
import pytest

from tests.conftest import MCP_SERVER_URL

pytestmark = pytest.mark.integration

CALL_TIMEOUT = 300

# ── 路径验证正则 ───────────────────────────────────────────────

TIMESTAMP_DIR_PATTERN = re.compile(r'/user_custom_data/\d{8}_\d{6}_batch_\d{3}/parquet/')
TIMESTAMP_FILE_PATTERN = re.compile(r'/\d{8}_\d{6}\.parquet$')


def _parse_hdfs_path(text: str) -> dict | None:
    m = re.search(r'(/[\w\-_/]+\.parquet)', text)
    if not m:
        return None
    full_path = m.group(1)
    dir_part, file_part = full_path.rsplit("/", 1)
    return {"full": full_path, "dir": dir_part + "/", "file": file_part}


def _verify_path_structure(text: str, mode: str) -> bool:
    """根据路径结构特征验证 LLM 解析是否正确

    - default:   目录=自动  文件名=自动
    - filename:  目录=自动  文件名=自定义
    - directory: 目录=自定义  文件名=自动
    - fullpath:  目录=自定义  文件名=自定义
    """
    parsed = _parse_hdfs_path(text)
    if not parsed:
        return False

    dir_is_auto = bool(TIMESTAMP_DIR_PATTERN.search(parsed["dir"]))
    file_is_auto = bool(TIMESTAMP_FILE_PATTERN.search(parsed["full"]))

    expected = {
        "default":   (True,  True),
        "filename":  (True,  False),
        "directory": (False, True),
        "fullpath":  (False, False),
    }
    exp_dir, exp_file = expected[mode]
    return dir_is_auto == exp_dir and file_is_auto == exp_file


# ── 测试用例 ───────────────────────────────────────────────────

NL_HDFS_CASES = [
    # 默认路径
    {"label": "默认-1", "query": "查询abortspanbytes表格的前30条数据，将结果上传到HDFS", "mode": "default"},
    {"label": "默认-2", "query": "帮我查abortspanbytes表的前30条记录并上传HDFS", "mode": "default"},
    # 纯文件名
    {"label": "文件名-1", "query": "查询abortspanbytes表格的前30条数据，将文件命名为my_custom_report上传到HDFS", "mode": "filename"},
    {"label": "文件名-2", "query": "查询abortspanbytes表格的前30条数据，文件名为special_export并上传到HDFS", "mode": "filename"},
    # 目录路径
    {"label": "目录-1", "query": "查询abortspanbytes表格的前30条数据，上传到HDFS的test_batch目录下", "mode": "directory"},
    {"label": "目录-2", "query": "查询abortspanbytes表格的前30条数据，保存在project_a文件夹下并上传HDFS", "mode": "directory"},
    # 完整路径
    {"label": "完整-1", "query": "查询abortspanbytes表格的前30条数据，保存到project_a/daily_report并上传HDFS", "mode": "fullpath"},
    {"label": "完整-2", "query": "帮我查abortspanbytes表前30条数据，导出到HDFS，放在reports/2025/summary_report这个路径下", "mode": "fullpath"},
]


async def _call_hdfs_nl(query: str, session_id: str = "") -> str:
    from fastmcp.client import Client
    async with Client(MCP_SERVER_URL) as client:
        args = {"query": query}
        if session_id:
            args["session_id"] = session_id
        result = await asyncio.wait_for(
            client.call_tool("query_and_upload_to_hdfs", args),
            timeout=CALL_TIMEOUT,
        )
        text = "".join(item.text for item in result.content if hasattr(item, "text"))
        return text


class TestNaturalLanguageHdfsPath:

    @pytest.mark.parametrize("case", NL_HDFS_CASES, ids=[c["label"] for c in NL_HDFS_CASES])
    def test_nl_hdfs_path(self, case):
        text = asyncio.get_event_loop().run_until_complete(
            _call_hdfs_nl(case["query"])
        )
        assert "错误" not in text and "失败" not in text, f"查询失败: {text[:200]}"
        assert _verify_path_structure(text, case["mode"]), (
            f"路径结构不符合 [{case['mode']}] 模式: {text[:300]}"
        )
