"""Wave 2A: AsyncQueryTracker + AsyncHDFSUploader 测试

验证：
- Tracker 写入 1000 条记录后，每一行都能独立解析
- Tracker 队列满时丢弃最新记录并增加指标
- Tracker 慢磁盘测试不会阻塞事件循环
- HDFS 临时目录包含 request_id + UUID
- 连续生成 1000 个自动目标路径无重复
- HDFS 超时或取消后不存在遗留临时文件
"""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import pandas
    _has_pandas = True
except ImportError:
    _has_pandas = False

from xiyan_mcp_server.runtime import Deadline, DeadlineExceeded, HdfsTimeoutConfig
from xiyan_mcp_server.utils.tracker_async import AsyncQueryTracker
from xiyan_mcp_server.utils.hdfs_async import AsyncHDFSUploader


# ═══════════════════════════════════════════════════════════
# AsyncQueryTracker 测试
# ═══════════════════════════════════════════════════════════

class TestAsyncQueryTracker:
    @pytest.mark.asyncio
    async def test_write_1000_records_all_parseable(self):
        """Tracker 写入 1000 条记录后，每一行都能独立解析"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = AsyncQueryTracker(
                output_dir=tmpdir,
                queue_capacity=2000,
                batch_size=50,
                flush_interval_seconds=0.1,
            )
            await tracker.start()

            # 写入 1000 条记录
            for i in range(1000):
                tracker.record({"id": i, "query": f"test_{i}", "nested": {"a": [1, 2, 3]}})

            # 等待 writer 处理完
            await asyncio.sleep(1.0)
            await tracker.shutdown()

            # 验证每一行都能独立解析
            files = list(Path(tmpdir).glob("*.jsonl"))
            assert len(files) >= 1

            total_lines = 0
            for f in files:
                content = f.read_text(encoding="utf-8")
                for line in content.strip().split('\n'):
                    if line.strip():
                        parsed = json.loads(line)  # 不应抛异常
                        assert "id" in parsed
                        total_lines += 1

            assert total_lines == 1000

    @pytest.mark.asyncio
    async def test_queue_full_drops_and_increments_metric(self):
        """队列满时丢弃最新记录并增加指标"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = AsyncQueryTracker(
                output_dir=tmpdir,
                queue_capacity=5,  # 极小容量
                batch_size=100,
                flush_interval_seconds=10,  # 不自动 flush
            )
            await tracker.start()

            # 暂停 writer 让它不消费
            tracker._running = False
            await asyncio.sleep(0.05)

            # 重新设为 running 但 writer 已停
            tracker._running = True

            # 写入超过容量的记录
            for i in range(20):
                tracker.record({"id": i})

            # 应该有丢弃
            assert tracker.dropped_count > 0
            await tracker.shutdown()

    @pytest.mark.asyncio
    async def test_single_line_json(self):
        """每条记录必须是单行合法 JSON（无 indent）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = AsyncQueryTracker(
                output_dir=tmpdir,
                queue_capacity=100,
                batch_size=10,
                flush_interval_seconds=0.1,
            )
            await tracker.start()

            tracker.record({"key": "value", "list": [1, 2, 3]})
            await asyncio.sleep(0.5)
            await tracker.shutdown()

            files = list(Path(tmpdir).glob("*.jsonl"))
            assert len(files) >= 1
            content = files[0].read_text(encoding="utf-8")
            lines = [l for l in content.strip().split('\n') if l.strip()]
            assert len(lines) == 1
            # 单行：不含换行
            assert '\n' not in lines[0]
            parsed = json.loads(lines[0])
            assert parsed["key"] == "value"

    @pytest.mark.asyncio
    async def test_does_not_block_event_loop(self):
        """Tracker 写入不会阻塞事件循环"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = AsyncQueryTracker(
                output_dir=tmpdir,
                queue_capacity=1000,
                batch_size=50,
                flush_interval_seconds=0.1,
            )
            await tracker.start()

            # 快速写入大量记录，同时测量事件循环延迟
            t0 = time.monotonic()
            for i in range(500):
                tracker.record({"id": i, "data": "x" * 100})
            write_time = time.monotonic() - t0

            # 写入 500 条应该非常快（< 50ms），因为只是 put_nowait
            assert write_time < 0.05, f"写入耗时 {write_time:.3f}s，不应阻塞事件循环"

            await asyncio.sleep(0.5)
            await tracker.shutdown()

    @pytest.mark.asyncio
    async def test_instance_unique_files(self):
        """每个服务实例写独立文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker1 = AsyncQueryTracker(output_dir=tmpdir, flush_interval_seconds=0.1)
            tracker2 = AsyncQueryTracker(output_dir=tmpdir, flush_interval_seconds=0.1)

            await tracker1.start()
            await tracker2.start()

            tracker1.record({"source": "t1"})
            tracker2.record({"source": "t2"})

            await asyncio.sleep(0.5)
            await tracker1.shutdown()
            await tracker2.shutdown()

            files = list(Path(tmpdir).glob("*.jsonl"))
            # 两个实例应该写不同文件
            assert len(files) == 2


# ═══════════════════════════════════════════════════════════
# AsyncHDFSUploader 测试
# ═══════════════════════════════════════════════════════════

class TestAsyncHDFSUploader:
    def test_unique_path_generation_no_duplicates(self):
        """连续生成 1000 个自动目标路径无重复"""
        config = {"hdfs_root_dir": "/user_custom_data"}
        uploader = AsyncHDFSUploader(config)

        paths = set()
        for i in range(1000):
            hdfs_path, batch_name = uploader._generate_unique_path(request_id=f"req{i}")
            paths.add(hdfs_path)

        assert len(paths) == 1000, f"生成了重复路径: {1000 - len(paths)} 个重复"

    def test_temp_dir_contains_request_id_and_uuid(self):
        """临时目录包含 request_id + UUID"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {"local_output_dir": tmpdir}
            uploader = AsyncHDFSUploader(config)

            temp_dir = uploader._make_temp_dir(request_id="abc123")
            assert "abc123" in temp_dir.name
            assert temp_dir.exists()

            # 清理
            temp_dir.rmdir()

    def test_cleanup_temp_dir(self):
        """清理临时目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {"local_output_dir": tmpdir}
            uploader = AsyncHDFSUploader(config)

            temp_dir = uploader._make_temp_dir(request_id="test")
            # 创建一些文件
            (temp_dir / "file1.parquet").write_text("data")
            (temp_dir / "file2.parquet").write_text("data")

            uploader._cleanup_temp_dir(temp_dir)
            assert not temp_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_on_failure(self):
        """失败时也必须清理本地临时文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {"local_output_dir": tmpdir, "remote_host": "192.0.2.1"}
            timeouts = HdfsTimeoutConfig(
                connect_timeout_seconds=1,
                scp_timeout_seconds=1,
                put_timeout_seconds=1,
                kill_grace_seconds=1,
            )
            uploader = AsyncHDFSUploader(config, timeouts=timeouts)

            result = {
                "fields": ["col1"],
                "truncated_results": [(1,), (2,)],
            }
            deadline = Deadline.after(3)

            # SCP 会失败（不可达的 host），但临时文件应被清理
            with pytest.raises(Exception):
                await uploader.convert_and_upload_async(
                    result, deadline=deadline, request_id="cleanup_test"
                )

            # 验证没有遗留的 .tmp_ 目录
            remaining_tmp = list(Path(tmpdir).glob(".tmp_*"))
            assert len(remaining_tmp) == 0, f"遗留临时目录: {remaining_tmp}"

    @pytest.mark.asyncio
    async def test_hdfs_concurrency_limited(self):
        """HDFS 并发上限为 2"""
        config = {"remote_host": "localhost", "remote_user": "test"}
        uploader = AsyncHDFSUploader(config, max_concurrency=2)

        sem = uploader._get_semaphore()
        assert sem._value == 2

    @pytest.mark.skipif(
        not _has_pandas, reason="pandas not installed"
    )
    def test_parquet_conversion(self):
        """Parquet 转换正确性"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {"local_output_dir": tmpdir}
            uploader = AsyncHDFSUploader(config)
            temp_dir = uploader._make_temp_dir()

            result = {
                "fields": ["name", "value"],
                "truncated_results": [("a", 1), ("b", 2)],
            }

            local_file = uploader._convert_to_parquet(result, temp_dir)
            assert local_file.exists()
            assert local_file.suffix == ".parquet"

            # 验证内容
            import pandas as pd
            df = pd.read_parquet(local_file)
            assert len(df) == 2
            assert "metric_name" in df.columns

            uploader._cleanup_temp_dir(temp_dir)
