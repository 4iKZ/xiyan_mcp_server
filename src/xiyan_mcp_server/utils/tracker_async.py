"""异步 Query Tracker

改造要点：
- 使用容量为 1000 的 asyncio.Queue
- 单后台 writer 写 JSONL
- 队列满时丢弃最新记录并增加指标、限频告警
- 每条记录必须是单行合法 JSON
- 默认 batch size 为 50，flush interval 为 1 秒
- 每个服务实例写独立文件，避免副本间争用
- 关闭时最多等待 5 秒完成 flush
"""

import asyncio
import datetime
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("xiyan_mcp_server.tracker_async")


class AsyncQueryTracker:
    """异步查询追踪器

    使用 asyncio.Queue 缓冲记录，单后台协程批量写入 JSONL。
    队列满时丢弃最新记录（不阻塞主流程）。
    """

    def __init__(
        self,
        output_dir: str = "/data/xiyan_mcp_server/query_tracker_logs",
        queue_capacity: int = 1000,
        batch_size: int = 50,
        flush_interval_seconds: float = 1.0,
        shutdown_flush_seconds: float = 5.0,
    ):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._queue_capacity = queue_capacity
        self._batch_size = batch_size
        self._flush_interval = flush_interval_seconds
        self._shutdown_flush_seconds = shutdown_flush_seconds

        # 每个服务实例写独立文件
        self._instance_id = f"{os.getpid()}_{uuid.uuid4().hex[:6]}"

        self._queue: Optional[asyncio.Queue] = None
        self._writer_task: Optional[asyncio.Task] = None
        self._running = False
        self._enabled = True

        # 指标
        self._written_count = 0
        self._dropped_count = 0
        self._last_drop_warning_time = 0.0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def written_count(self) -> int:
        return self._written_count

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    async def start(self) -> None:
        """启动后台 writer 协程"""
        if self._running:
            return
        self._queue = asyncio.Queue(maxsize=self._queue_capacity)
        self._running = True
        self._writer_task = asyncio.create_task(self._writer_loop())
        logger.info(
            f"AsyncQueryTracker 已启动: instance={self._instance_id}, "
            f"capacity={self._queue_capacity}, batch={self._batch_size}"
        )

    def record(self, record: Dict[str, Any]) -> None:
        """非阻塞地提交一条记录

        队列满时丢弃并增加指标（不阻塞调用方）。
        每条记录必须是单行合法 JSON。
        """
        if not self._enabled or not self._running:
            return

        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._dropped_count += 1
            # 限频告警：每 10 秒最多一次
            import time
            now = time.monotonic()
            if now - self._last_drop_warning_time > 10:
                logger.warning(
                    f"Tracker 队列已满，丢弃记录 (累计丢弃: {self._dropped_count})"
                )
                self._last_drop_warning_time = now

    async def _writer_loop(self) -> None:
        """后台 writer：批量从队列取记录并写入文件"""
        while self._running or (self._queue and not self._queue.empty()):
            batch: List[Dict] = []

            try:
                # 等待第一条记录（最多等 flush_interval）
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=self._flush_interval
                    )
                    batch.append(item)
                except asyncio.TimeoutError:
                    continue

                # 尝试凑满 batch
                while len(batch) < self._batch_size:
                    try:
                        item = self._queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break

                # 写入文件
                if batch:
                    await asyncio.to_thread(self._write_batch, batch)
                    self._written_count += len(batch)

            except asyncio.CancelledError:
                # 关闭时 flush 剩余
                await self._flush_remaining()
                raise
            except Exception as e:
                logger.error(f"Tracker writer 异常: {e}")
                await asyncio.sleep(0.1)

    def _write_batch(self, batch: List[Dict]) -> None:
        """批量写入 JSONL 文件（同步，在线程中执行）"""
        date_str = datetime.date.today().isoformat()
        file_name = f"query_tracker_{date_str}_{self._instance_id}.jsonl"
        file_path = self._output_dir / file_name

        lines = []
        for record in batch:
            try:
                # 每条记录必须是单行合法 JSON
                lines.append(json.dumps(record, ensure_ascii=False, separators=(',', ':')))
            except (TypeError, ValueError) as e:
                logger.warning(f"Tracker 记录序列化失败: {e}")

        if lines:
            with open(file_path, 'a', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')

    async def _flush_remaining(self) -> None:
        """关闭时 flush 队列中剩余记录"""
        if not self._queue:
            return

        batch: List[Dict] = []
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                batch.append(item)
            except asyncio.QueueEmpty:
                break

        if batch:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._write_batch, batch),
                    timeout=self._shutdown_flush_seconds,
                )
                self._written_count += len(batch)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Tracker shutdown flush 超时，丢弃 {len(batch)} 条记录"
                )

    async def shutdown(self) -> None:
        """关闭 tracker，最多等待 shutdown_flush_seconds 完成 flush"""
        self._running = False
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await asyncio.wait_for(
                    self._writer_task,
                    timeout=self._shutdown_flush_seconds,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._writer_task = None
        logger.info(
            f"AsyncQueryTracker 已关闭: written={self._written_count}, "
            f"dropped={self._dropped_count}"
        )


# ═══════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════

import threading

_async_tracker: Optional[AsyncQueryTracker] = None
_async_tracker_lock = threading.Lock()


def get_async_tracker() -> Optional[AsyncQueryTracker]:
    return _async_tracker


def init_async_tracker(
    output_dir: str = "/data/xiyan_mcp_server/query_tracker_logs",
    queue_capacity: int = 1000,
    batch_size: int = 50,
    flush_interval_seconds: float = 1.0,
    shutdown_flush_seconds: float = 5.0,
) -> AsyncQueryTracker:
    global _async_tracker
    with _async_tracker_lock:
        if _async_tracker is None:
            _async_tracker = AsyncQueryTracker(
                output_dir=output_dir,
                queue_capacity=queue_capacity,
                batch_size=batch_size,
                flush_interval_seconds=flush_interval_seconds,
                shutdown_flush_seconds=shutdown_flush_seconds,
            )
    return _async_tracker


async def shutdown_async_tracker() -> None:
    global _async_tracker
    with _async_tracker_lock:
        tracker = _async_tracker
        _async_tracker = None
    if tracker is not None:
        await tracker.shutdown()
