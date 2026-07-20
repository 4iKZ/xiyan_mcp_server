"""Wave 0 公共契约单元测试

验证 Deadline、错误码、RuntimeConfig、RequestAdmission、RuntimeController 的行为。
"""

import asyncio
import time

import pytest

from xiyan_mcp_server.runtime import (
    Deadline,
    DeadlineExceeded,
    ErrorCode,
    ERROR_MESSAGES,
    ServiceError,
    error_response,
    RuntimeConfig,
    ConcurrencyConfig,
    DatabasePoolConfig,
    RequestAdmission,
    RuntimeController,
)


# ═══════════════════════════════════════════════════════════
# Deadline 测试
# ═══════════════════════════════════════════════════════════

class TestDeadline:
    def test_after_creates_future_deadline(self):
        d = Deadline.after(10.0)
        assert not d.expired()
        assert d.remaining() > 9.0
        assert d.remaining() <= 10.0

    def test_expired_deadline(self):
        d = Deadline.after(0.0)
        # 0 秒 deadline 应立即过期（或极其接近）
        time.sleep(0.001)
        assert d.expired()
        assert d.remaining() == 0.0

    def test_check_raises_when_expired(self):
        d = Deadline.after(0.0)
        time.sleep(0.001)
        with pytest.raises(DeadlineExceeded):
            d.check()

    def test_check_passes_when_not_expired(self):
        d = Deadline.after(10.0)
        d.check()  # 不应抛出

    def test_remaining_decreases_over_time(self):
        d = Deadline.after(5.0)
        r1 = d.remaining()
        time.sleep(0.05)
        r2 = d.remaining()
        assert r2 < r1

    @pytest.mark.asyncio
    async def test_wait_or_timeout_success(self):
        d = Deadline.after(5.0)

        async def quick():
            await asyncio.sleep(0.01)
            return "done"

        result = await d.wait_or_timeout(quick())
        assert result == "done"

    @pytest.mark.asyncio
    async def test_wait_or_timeout_expires(self):
        d = Deadline.after(0.05)

        async def slow():
            await asyncio.sleep(10)
            return "never"

        with pytest.raises(DeadlineExceeded):
            await d.wait_or_timeout(slow())

    @pytest.mark.asyncio
    async def test_wait_or_timeout_already_expired(self):
        d = Deadline.after(0.0)
        time.sleep(0.001)

        with pytest.raises(DeadlineExceeded):
            await d.wait_or_timeout(asyncio.sleep(0))


# ═══════════════════════════════════════════════════════════
# 错误码测试
# ═══════════════════════════════════════════════════════════

class TestErrorCodes:
    def test_error_messages_format(self):
        assert "OVERLOADED" in ERROR_MESSAGES[ErrorCode.OVERLOADED]
        assert "DEADLINE_EXCEEDED" in ERROR_MESSAGES[ErrorCode.DEADLINE_EXCEEDED]
        assert "SERVER_DRAINING" in ERROR_MESSAGES[ErrorCode.SERVER_DRAINING]

    def test_error_response(self):
        resp = error_response(ErrorCode.OVERLOADED)
        assert resp == "错误[OVERLOADED]: 服务繁忙，请稍后重试"

    def test_service_error_carries_code(self):
        err = ServiceError(ErrorCode.OVERLOADED)
        assert err.code == ErrorCode.OVERLOADED
        assert "OVERLOADED" in str(err)

    def test_service_error_with_detail(self):
        err = ServiceError(ErrorCode.DEADLINE_EXCEEDED, detail="get_data 180s")
        assert "get_data 180s" in str(err)


# ═══════════════════════════════════════════════════════════
# RuntimeConfig 测试
# ═══════════════════════════════════════════════════════════

class TestRuntimeConfig:
    def test_defaults(self):
        cfg = RuntimeConfig()
        assert cfg.max_in_flight == 4
        assert cfg.max_waiters == 8
        assert cfg.admission_wait_seconds == 5
        assert cfg.get_data_deadline_seconds == 180
        assert cfg.hdfs_deadline_seconds == 300
        assert cfg.concurrency.llm == 2
        assert cfg.concurrency.sql == 4
        assert cfg.database.pool_size == 6
        assert cfg.tracker.queue_capacity == 1000

    def test_from_dict_empty(self):
        cfg = RuntimeConfig.from_dict({})
        assert cfg.max_in_flight == 4
        cfg.validate()  # 默认值应合法

    def test_from_dict_partial(self):
        cfg = RuntimeConfig.from_dict({
            "runtime": {"max_in_flight": 2, "max_waiters": 4},
            "concurrency": {"llm": 3},
        })
        assert cfg.max_in_flight == 2
        assert cfg.max_waiters == 4
        assert cfg.concurrency.llm == 3
        assert cfg.concurrency.sql == 4  # 默认值

    def test_validate_rejects_invalid(self):
        cfg = RuntimeConfig(max_in_flight=0)
        with pytest.raises(ValueError, match="max_in_flight"):
            cfg.validate()

    def test_database_pool_validation_pass(self):
        db = DatabasePoolConfig(pool_size=6, max_overflow=0)
        db.validate()  # 6 >= 4 + 2

    def test_database_pool_validation_fail(self):
        db = DatabasePoolConfig(pool_size=3, max_overflow=0)
        with pytest.raises(ValueError, match="连接池容量不足"):
            db.validate()


# ═══════════════════════════════════════════════════════════
# RequestAdmission 测试
# ═══════════════════════════════════════════════════════════

class TestRequestAdmission:
    @pytest.mark.asyncio
    async def test_basic_acquire_release(self):
        cfg = RuntimeConfig(max_in_flight=2, max_waiters=4, admission_wait_seconds=1)
        admission = RequestAdmission(cfg)

        await admission.acquire()
        assert admission.active_count == 1
        admission.release()
        assert admission.active_count == 0

    @pytest.mark.asyncio
    async def test_concurrency_limited(self):
        """验证活跃请求不超过 max_in_flight"""
        cfg = RuntimeConfig(max_in_flight=2, max_waiters=4, admission_wait_seconds=2)
        admission = RequestAdmission(cfg)
        peak = 0
        lock = asyncio.Lock()

        async def worker():
            nonlocal peak
            await admission.acquire()
            async with lock:
                current = admission.active_count
                if current > peak:
                    peak = current
            await asyncio.sleep(0.1)
            admission.release()

        await asyncio.gather(*[worker() for _ in range(6)])
        assert peak <= 2

    @pytest.mark.asyncio
    async def test_overloaded_when_queue_full(self):
        """第 max_in_flight + max_waiters + 1 个请求立即返回 OVERLOADED"""
        cfg = RuntimeConfig(max_in_flight=1, max_waiters=1, admission_wait_seconds=5)
        admission = RequestAdmission(cfg)

        # 占满执行槽
        await admission.acquire()

        # 第一个等待者进入队列
        waiter_task = asyncio.create_task(admission.acquire())
        await asyncio.sleep(0.05)  # 让 waiter 进入等待

        # 第二个等待者应被拒绝（队列已满）
        with pytest.raises(ServiceError) as exc_info:
            await admission.acquire()
        assert exc_info.value.code == ErrorCode.OVERLOADED

        # 清理
        admission.release()
        await waiter_task

    @pytest.mark.asyncio
    async def test_admission_timeout(self):
        """排队超时返回 OVERLOADED"""
        cfg = RuntimeConfig(max_in_flight=1, max_waiters=8, admission_wait_seconds=0.1)
        admission = RequestAdmission(cfg)

        await admission.acquire()  # 占满

        with pytest.raises(ServiceError) as exc_info:
            await admission.acquire()  # 等待 0.1s 后超时
        assert exc_info.value.code == ErrorCode.OVERLOADED

        admission.release()

    @pytest.mark.asyncio
    async def test_draining_rejects_new_requests(self):
        cfg = RuntimeConfig()
        admission = RequestAdmission(cfg)
        admission.start_draining()

        with pytest.raises(ServiceError) as exc_info:
            await admission.acquire()
        assert exc_info.value.code == ErrorCode.SERVER_DRAINING

    @pytest.mark.asyncio
    async def test_13th_concurrent_request_rejected(self):
        """验证：4 活跃 + 8 排队 = 12，第 13 个立即拒绝"""
        cfg = RuntimeConfig(max_in_flight=4, max_waiters=8, admission_wait_seconds=10)
        admission = RequestAdmission(cfg)

        # 占满 4 个执行槽
        for _ in range(4):
            await admission.acquire()

        # 8 个等待者进入队列
        waiters = []
        for _ in range(8):
            t = asyncio.create_task(admission.acquire())
            waiters.append(t)
        await asyncio.sleep(0.05)  # 让 waiters 进入等待状态

        # 第 13 个应被立即拒绝
        with pytest.raises(ServiceError) as exc_info:
            await admission.acquire()
        assert exc_info.value.code == ErrorCode.OVERLOADED

        # 清理：释放所有槽位
        for _ in range(4):
            admission.release()
        for t in waiters:
            await t
            admission.release()


# ═══════════════════════════════════════════════════════════
# RuntimeController 测试
# ═══════════════════════════════════════════════════════════

class TestRuntimeController:
    def test_initial_state(self):
        cfg = RuntimeConfig()
        rc = RuntimeController(cfg)
        assert rc.is_alive
        assert not rc.is_ready  # 未 mark_ready

    def test_ready_after_mark(self):
        cfg = RuntimeConfig()
        rc = RuntimeController(cfg)
        rc.mark_ready()
        assert rc.is_ready

    def test_not_ready_when_draining(self):
        cfg = RuntimeConfig()
        rc = RuntimeController(cfg)
        rc.mark_ready()
        rc.admission.start_draining()
        assert not rc.is_ready

    def test_not_ready_when_init_failed(self):
        cfg = RuntimeConfig()
        rc = RuntimeController(cfg)
        rc.mark_init_failed("Redis 连接失败")
        rc.mark_ready()
        assert not rc.is_ready

    def test_metrics_snapshot(self):
        cfg = RuntimeConfig()
        rc = RuntimeController(cfg)
        rc.mark_ready()
        rc.record_stage_duration("schema", 50.0)
        rc.record_stage_duration("schema", 100.0)
        rc.record_deadline_exceeded()

        snap = rc.get_metrics_snapshot()
        assert snap["active_requests"] == 0
        assert snap["deadline_exceeded"] == 1
        assert snap["stage_durations_avg_ms"]["schema"] == 75.0
        assert snap["ready"] is True

    @pytest.mark.asyncio
    async def test_lag_monitor(self):
        cfg = RuntimeConfig()
        rc = RuntimeController(cfg)
        await rc.start_lag_monitor()
        await asyncio.sleep(0.5)
        snap = rc.get_metrics_snapshot()
        # lag 应该是合理的小值
        assert snap["event_loop_lag_p99_ms"] < 100
        await rc.shutdown()
