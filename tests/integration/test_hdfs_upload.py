"""query_and_upload_to_hdfs 工具集成测试

覆盖四种 hdfs_path 模式：
  1. 默认路径（空 hdfs_path）
  2. 纯文件名（无 /）
  3. 目录路径（以 / 结尾）
  4. 完整路径（目录/文件名）
以及 session_id 向后兼容。

需要 MCP Server 运行中；不可达时自动 skip。
"""

import asyncio
import pytest

from tests.conftest import MCP_SERVER_URL

pytestmark = pytest.mark.integration

TEST_QUERY = "查询abortspanbytes表格的前30条数据"
CALL_TIMEOUT = 300  # HDFS 上传可能耗时较长


async def _call_hdfs_upload(query: str, session_id: str = "", hdfs_path: str = "") -> str:
    from fastmcp.client import Client
    async with Client(MCP_SERVER_URL) as client:
        args = {"query": query}
        if session_id:
            args["session_id"] = session_id
        if hdfs_path:
            args["hdfs_path"] = hdfs_path

        result = await asyncio.wait_for(
            client.call_tool("query_and_upload_to_hdfs", args),
            timeout=CALL_TIMEOUT,
        )
        assert not result.is_error, f"HDFS 上传返回错误: {result.content}"
        text = "".join(item.text for item in result.content if hasattr(item, "text"))
        assert text, "返回内容为空"
        return text


class TestHdfsUploadModes:

    def test_default_path(self):
        """模式1: 空 hdfs_path → 全自动时间戳"""
        text = asyncio.get_event_loop().run_until_complete(
            _call_hdfs_upload(TEST_QUERY)
        )
        assert ".parquet" in text

    def test_pure_filename(self):
        """模式2: 纯文件名 → 时间戳目录 + 自定义文件名"""
        text = asyncio.get_event_loop().run_until_complete(
            _call_hdfs_upload(TEST_QUERY, hdfs_path="my_custom_report")
        )
        assert ".parquet" in text

    def test_directory_path(self):
        """模式3: 目录/ → 自定义目录 + 自动时间戳文件名"""
        text = asyncio.get_event_loop().run_until_complete(
            _call_hdfs_upload(TEST_QUERY, hdfs_path="test_batch/")
        )
        assert ".parquet" in text

    def test_full_path(self):
        """模式4: 目录/文件名 → 自定义完整路径"""
        text = asyncio.get_event_loop().run_until_complete(
            _call_hdfs_upload(TEST_QUERY, hdfs_path="project_a/daily_report")
        )
        assert ".parquet" in text


class TestHdfsBackwardCompat:

    def test_session_id_compat(self):
        """session_id 向后兼容"""
        text = asyncio.get_event_loop().run_until_complete(
            _call_hdfs_upload(TEST_QUERY, session_id="backward_compat_test")
        )
        assert ".parquet" in text
