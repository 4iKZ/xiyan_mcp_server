"""Wave 1A: SqlExecutionManager 并发与取消安全测试

验证：
- 两个各耗时 300ms 的 SQL 并发调用总耗时 < 400ms
- 四个各耗时 300ms 的 SQL 并发调用总耗时 < 500ms
- 峰值 SQL 活跃线程不超过 4
- 超时任务不会提前释放并发槽
"""

import asyncio
import time
import threading
from unittest.mock import MagicMock, patch

import pytest

from xiyan_mcp_server.runtime import Deadline, DeadlineExceeded
from xiyan_mcp_server.utils.sql_executor import (
    SqlExecutionManager,
    QueryResult,
    init_sql_manager,
    get_sql_manager,
    shutdown_sql_manager,
)


# ═══════════════════════════════════════════════════════════
# 辅助：模拟慢 SQL 的 engine
# ═══════════════════════════════════════════════════════════

def make_slow_engine(delay: float = 0.3):
    """创建一个模拟的 engine，execute 时 sleep delay 秒"""
    engine = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.keys.return_value = ["col1", "col2"]
    cursor.fetchall.return_value = [(1, "a"), (2, "b")]
    conn.execute.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.connection.dbapi_connection = MagicMock()
    engine.connect.return_value = conn
    engine.dialect.name = "postgresql"

    # 让 execute 实际 sleep
    original_execute = conn.execute

    def slow_execute(*args, **kwargs):
        time.sleep(delay)
        return cursor

    conn.execute = slow_execute
    return engine


def make_blocking_engine(block_event: threading.Event):
    """创建一个阻塞的 engine，直到 block_event 被 set"""
    engine = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.keys.return_value = ["col1"]
    cursor.fetchall.return_value = [(1,)]
    conn.connection.dbapi_connection = MagicMock()
    engine.connect.return_value = conn
    engine.dialect.name = "postgresql"

    def blocking_execute(*args, **kwargs):
        block_event.wait(timeout=30)
        return cursor

    conn.execute = blocking_execute
    return engine


# ═══════════════════════════════════════════════════════════
# 并发性能测试
# ═══════════════════════════════════════════════════════════

class TestSqlConcurrency:
    @pytest.mark.asyncio
    async def test_two_concurrent_300ms_under_400ms(self):
        """两个各耗时 300ms 的 SQL 并发调用总耗时 < 400ms"""
        manager = SqlExecutionManager(sql_concurrency=4, query_timeout_seconds=10)
        engine = make_slow_engine(0.3)
        deadline = Deadline.after(10)

        t0 = time.monotonic()
        results = await asyncio.gather(
            manager.execute_async(engine, "SELECT 1;", deadline=deadline),
            manager.execute_async(engine, "SELECT 2;", deadline=deadline),
        )
        elapsed = time.monotonic() - t0

        assert elapsed < 0.4, f"两个并发 300ms 查询耗时 {elapsed:.3f}s，应 < 0.4s"
        assert all(r.row_count == 2 for r in results)
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_four_concurrent_300ms_under_500ms(self):
        """四个各耗时 300ms 的 SQL 并发调用总耗时 < 500ms"""
        manager = SqlExecutionManager(sql_concurrency=4, query_timeout_seconds=10)
        engine = make_slow_engine(0.3)
        deadline = Deadline.after(10)

        t0 = time.monotonic()
        results = await asyncio.gather(
            *[manager.execute_async(engine, f"SELECT {i};", deadline=deadline) for i in range(4)]
        )
        elapsed = time.monotonic() - t0

        assert elapsed < 0.5, f"四个并发 300ms 查询耗时 {elapsed:.3f}s，应 < 0.5s"
        assert len(results) == 4
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_peak_active_never_exceeds_4(self):
        """峰值 SQL 活跃线程不超过 4"""
        manager = SqlExecutionManager(sql_concurrency=4, query_timeout_seconds=10)
        engine = make_slow_engine(0.1)
        deadline = Deadline.after(10)
        peak = 0
        peak_lock = asyncio.Lock()

        original_execute = manager._execute_sync

        def tracked_execute(*args, **kwargs):
            nonlocal peak
            # 记录活跃数
            current = manager.active_count
            if current > peak:
                peak = current
            return original_execute(*args, **kwargs)

        manager._execute_sync = tracked_execute

        # 发起 8 个并发请求
        await asyncio.gather(
            *[manager.execute_async(engine, f"SELECT {i};", deadline=deadline) for i in range(8)]
        )

        assert peak <= 4, f"峰值活跃线程 {peak}，应 <= 4"
        await manager.shutdown()


# ═══════════════════════════════════════════════════════════
# 超时与取消安全测试
# ═══════════════════════════════════════════════════════════

class TestSqlTimeoutSafety:
    @pytest.mark.asyncio
    async def test_deadline_exceeded_raises(self):
        """deadline 过期时抛出 DeadlineExceeded"""
        manager = SqlExecutionManager(sql_concurrency=4, query_timeout_seconds=10)
        engine = make_slow_engine(2.0)  # 2 秒延迟
        deadline = Deadline.after(0.1)  # 100ms 截止

        with pytest.raises(DeadlineExceeded):
            await manager.execute_async(engine, "SELECT 1;", deadline=deadline)

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_timeout_does_not_prematurely_release_slot(self):
        """超时任务不会提前释放并发槽"""
        manager = SqlExecutionManager(sql_concurrency=1, slot_wait_seconds=3.0, query_timeout_seconds=10)
        block_event = threading.Event()
        engine = make_blocking_engine(block_event)
        deadline = Deadline.after(0.2)  # 200ms 后超时

        # 第一个请求会阻塞
        task1 = asyncio.create_task(
            manager.execute_async(engine, "SELECT 1;", deadline=deadline)
        )

        await asyncio.sleep(0.05)  # 让 task1 获取槽位

        # 第二个请求应该等待（槽位被占）
        deadline2 = Deadline.after(5.0)
        engine2 = make_slow_engine(0.01)

        task2 = asyncio.create_task(
            manager.execute_async(engine2, "SELECT 2;", deadline=deadline2)
        )

        # 等待 task1 超时（但线程仍在运行，槽位未释放）
        await asyncio.sleep(0.3)  # task1 的 deadline 已过

        # 此时 task2 不应完成（因为槽位要等 task1 的线程真正结束）
        assert not task2.done(), "task2 不应在 task1 线程完成前获取槽位"

        # 释放阻塞让 task1 的线程完成
        block_event.set()

        # task1 应抛出 DeadlineExceeded
        with pytest.raises(DeadlineExceeded):
            await task1

        # task2 现在应该能完成
        result = await asyncio.wait_for(task2, timeout=3.0)
        assert result.row_count == 2  # make_slow_engine mock 返回 2 行

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_slot_wait_timeout(self):
        """获取执行槽超时抛出 TimeoutError"""
        manager = SqlExecutionManager(
            sql_concurrency=1, slot_wait_seconds=0.1, query_timeout_seconds=10
        )
        block_event = threading.Event()
        engine = make_blocking_engine(block_event)
        deadline = Deadline.after(5.0)

        # 占满唯一的槽位
        task1 = asyncio.create_task(
            manager.execute_async(engine, "SELECT 1;", deadline=deadline)
        )
        await asyncio.sleep(0.05)

        # 第二个请求等待 0.1s 后应超时
        with pytest.raises(asyncio.TimeoutError, match="获取 SQL 执行槽超时"):
            await manager.execute_async(engine, "SELECT 2;", deadline=deadline)

        block_event.set()
        await task1
        await manager.shutdown()


# ═══════════════════════════════════════════════════════════
# 全局单例管理测试
# ═══════════════════════════════════════════════════════════

class TestSqlManagerSingleton:
    @pytest.mark.asyncio
    async def test_init_and_get(self):
        manager = init_sql_manager(sql_concurrency=2, query_timeout_seconds=30)
        assert get_sql_manager() is manager
        await shutdown_sql_manager()
        assert get_sql_manager() is None

    @pytest.mark.asyncio
    async def test_pool_stats(self):
        manager = SqlExecutionManager(sql_concurrency=4)
        stats = manager.pool_stats
        assert stats["active"] == 0
        assert stats["max_concurrency"] == 4
        await manager.shutdown()


# ═══════════════════════════════════════════════════════════
# max_rows 截断测试
# ═══════════════════════════════════════════════════════════

class TestMaxRows:
    @pytest.mark.asyncio
    async def test_result_truncated(self):
        """结果超过 max_rows 时被截断"""
        manager = SqlExecutionManager(sql_concurrency=4, query_timeout_seconds=10)

        engine = MagicMock()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.keys.return_value = ["id"]
        cursor.fetchall.return_value = [(i,) for i in range(100)]
        conn.execute.return_value = cursor
        conn.connection.dbapi_connection = MagicMock()
        engine.connect.return_value = conn
        engine.dialect.name = "postgresql"

        deadline = Deadline.after(10)
        result = await manager.execute_async(engine, "SELECT id FROM t;", deadline=deadline, max_rows=10)

        assert result.row_count == 10
        assert len(result.records) == 10
        await manager.shutdown()
