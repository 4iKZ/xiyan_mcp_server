"""运行时并发控制与公共契约模块

定义 Deadline、统一错误码、运行时配置模型、请求准入控制和运行时控制器。
所有阶段（Schema、LLM、SQL、HDFS）共享同一个绝对 monotonic deadline，
不得在每层重新计算完整超时时间。
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("xiyan_mcp_server.runtime")


# ═══════════════════════════════════════════════════════════
# Deadline：绝对截止时间传播
# ═══════════════════════════════════════════════════════════

class DeadlineExceeded(Exception):
    """请求处理超时"""
    pass


class Deadline:
    """绝对 monotonic 截止时间，跨层传播。

    所有阶段接收同一个 Deadline 实例，通过 remaining() 获取剩余时间，
    不得在每层重新计算完整超时。
    """

    __slots__ = ("_deadline",)

    def __init__(self, deadline: float):
        self._deadline = deadline

    @classmethod
    def after(cls, seconds: float) -> "Deadline":
        """创建从当前时刻起 seconds 秒后到期的 Deadline"""
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        """返回剩余秒数，已过期则返回 0.0"""
        r = self._deadline - time.monotonic()
        return max(0.0, r)

    def expired(self) -> bool:
        """是否已过期"""
        return time.monotonic() >= self._deadline

    def check(self) -> None:
        """如果已过期则抛出 DeadlineExceeded"""
        if self.expired():
            raise DeadlineExceeded("请求处理超时")

    async def wait_or_timeout(self, coro):
        """等待协程完成，超时则取消并抛出 DeadlineExceeded"""
        remaining = self.remaining()
        if remaining <= 0:
            raise DeadlineExceeded("请求处理超时")
        try:
            return await asyncio.wait_for(coro, timeout=remaining)
        except asyncio.TimeoutError:
            raise DeadlineExceeded("请求处理超时")


# ═══════════════════════════════════════════════════════════
# 统一错误码与错误响应
# ═══════════════════════════════════════════════════════════

class ErrorCode(str, Enum):
    OVERLOADED = "OVERLOADED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    SERVER_DRAINING = "SERVER_DRAINING"


ERROR_MESSAGES = {
    ErrorCode.OVERLOADED: "错误[OVERLOADED]: 服务繁忙，请稍后重试",
    ErrorCode.DEADLINE_EXCEEDED: "错误[DEADLINE_EXCEEDED]: 请求处理超时",
    ErrorCode.SERVER_DRAINING: "错误[SERVER_DRAINING]: 服务器正在关闭，暂时不接受新请求",
}


class ServiceError(Exception):
    """带错误码的服务异常，用于统一错误映射"""

    def __init__(self, code: ErrorCode, detail: Optional[str] = None):
        self.code = code
        self.detail = detail
        msg = ERROR_MESSAGES[code]
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


def error_response(code: ErrorCode) -> str:
    """生成统一错误响应文本"""
    return ERROR_MESSAGES[code]


# ═══════════════════════════════════════════════════════════
# 运行时配置模型
# ═══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ConcurrencyConfig:
    llm: int = 2
    schema_metadata: int = 2
    sql: int = 4
    hdfs: int = 2


@dataclass(frozen=True)
class DatabasePoolConfig:
    pool_size: int = 6
    max_overflow: int = 0
    pool_timeout_seconds: float = 5
    query_timeout_seconds: float = 60
    metadata_reserved_connections: int = 2

    def validate(self) -> None:
        """启动时校验连接池容量是否满足并发需求"""
        required = 4 + self.metadata_reserved_connections  # sql_concurrency + reserved
        available = self.pool_size + self.max_overflow
        if available < required:
            raise ValueError(
                f"连接池容量不足: pool_size({self.pool_size}) + max_overflow({self.max_overflow}) "
                f"= {available} < sql_concurrency(4) + metadata_reserved({self.metadata_reserved_connections}) "
                f"= {required}"
            )


@dataclass(frozen=True)
class HdfsTimeoutConfig:
    connect_timeout_seconds: float = 10
    scp_timeout_seconds: float = 60
    put_timeout_seconds: float = 120
    kill_grace_seconds: float = 2


@dataclass(frozen=True)
class TrackerConfig:
    queue_capacity: int = 1000
    batch_size: int = 50
    flush_interval_seconds: float = 1
    shutdown_flush_seconds: float = 5


@dataclass(frozen=True)
class ShutdownConfig:
    drain_seconds: float = 30
    cancellation_seconds: float = 5


@dataclass(frozen=True)
class MetricsConfig:
    enabled: bool = True


@dataclass(frozen=True)
class TransportConfig:
    stateless_http: bool = True


@dataclass(frozen=True)
class RuntimeConfig:
    """运行时总配置，从 YAML runtime/concurrency/database/hdfs/tracker/shutdown 节解析"""
    max_in_flight: int = 4
    max_waiters: int = 8
    admission_wait_seconds: float = 5
    get_data_deadline_seconds: float = 180
    hdfs_deadline_seconds: float = 300

    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    database: DatabasePoolConfig = field(default_factory=DatabasePoolConfig)
    hdfs_timeouts: HdfsTimeoutConfig = field(default_factory=HdfsTimeoutConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    shutdown: ShutdownConfig = field(default_factory=ShutdownConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    transport: TransportConfig = field(default_factory=TransportConfig)

    def validate(self) -> None:
        """启动时校验配置合法性，非法值立即报错"""
        if self.max_in_flight < 1:
            raise ValueError("runtime.max_in_flight 必须 >= 1")
        if self.max_waiters < 0:
            raise ValueError("runtime.max_waiters 必须 >= 0")
        if self.admission_wait_seconds <= 0:
            raise ValueError("runtime.admission_wait_seconds 必须 > 0")
        if self.get_data_deadline_seconds <= 0:
            raise ValueError("runtime.get_data_deadline_seconds 必须 > 0")
        if self.hdfs_deadline_seconds <= 0:
            raise ValueError("runtime.hdfs_deadline_seconds 必须 > 0")
        self.database.validate()
        # 交叉校验：连接池容量需满足实际 SQL 并发 + 元数据保留
        required = self.concurrency.sql + self.database.metadata_reserved_connections
        available = self.database.pool_size + self.database.max_overflow
        if available < required:
            raise ValueError(
                f"连接池容量不足: pool_size({self.database.pool_size}) + "
                f"max_overflow({self.database.max_overflow}) = {available} < "
                f"concurrency.sql({self.concurrency.sql}) + "
                f"metadata_reserved({self.database.metadata_reserved_connections}) = {required}"
            )

    @classmethod
    def from_dict(cls, cfg: dict) -> "RuntimeConfig":
        """从 YAML 配置字典解析，缺失字段使用默认值"""
        runtime = cfg.get("runtime", {}) or {}
        concurrency = cfg.get("concurrency", {}) or {}
        database = cfg.get("database_pool", {}) or {}
        hdfs = cfg.get("hdfs_timeouts", {}) or {}
        tracker = cfg.get("tracker", {}) or {}
        shutdown = cfg.get("shutdown", {}) or {}
        metrics = cfg.get("metrics", {}) or {}
        transport = cfg.get("transport", {}) or {}

        return cls(
            max_in_flight=int(runtime.get("max_in_flight", 4)),
            max_waiters=int(runtime.get("max_waiters", 8)),
            admission_wait_seconds=float(runtime.get("admission_wait_seconds", 5)),
            get_data_deadline_seconds=float(runtime.get("get_data_deadline_seconds", 180)),
            hdfs_deadline_seconds=float(runtime.get("hdfs_deadline_seconds", 300)),
            concurrency=ConcurrencyConfig(
                llm=int(concurrency.get("llm", 2)),
                schema_metadata=int(concurrency.get("schema_metadata", 2)),
                sql=int(concurrency.get("sql", 4)),
                hdfs=int(concurrency.get("hdfs", 2)),
            ),
            database=DatabasePoolConfig(
                pool_size=int(database.get("pool_size", 6)),
                max_overflow=int(database.get("max_overflow", 0)),
                pool_timeout_seconds=float(database.get("pool_timeout_seconds", 5)),
                query_timeout_seconds=float(database.get("query_timeout_seconds", 60)),
                metadata_reserved_connections=int(database.get("metadata_reserved_connections", 2)),
            ),
            hdfs_timeouts=HdfsTimeoutConfig(
                connect_timeout_seconds=float(hdfs.get("connect_timeout_seconds", 10)),
                scp_timeout_seconds=float(hdfs.get("scp_timeout_seconds", 60)),
                put_timeout_seconds=float(hdfs.get("put_timeout_seconds", 120)),
                kill_grace_seconds=float(hdfs.get("kill_grace_seconds", 2)),
            ),
            tracker=TrackerConfig(
                queue_capacity=int(tracker.get("queue_capacity", 1000)),
                batch_size=int(tracker.get("batch_size", 50)),
                flush_interval_seconds=float(tracker.get("flush_interval_seconds", 1)),
                shutdown_flush_seconds=float(tracker.get("shutdown_flush_seconds", 5)),
            ),
            shutdown=ShutdownConfig(
                drain_seconds=float(shutdown.get("drain_seconds", 30)),
                cancellation_seconds=float(shutdown.get("cancellation_seconds", 5)),
            ),
            metrics=MetricsConfig(
                enabled=bool(metrics.get("enabled", True)),
            ),
            transport=TransportConfig(
                stateless_http=bool(transport.get("stateless_http", True)),
            ),
        )


# ═══════════════════════════════════════════════════════════
# 请求准入控制
# ═══════════════════════════════════════════════════════════

class RequestAdmission:
    """请求准入控制器

    - 全局活跃请求上限 max_in_flight (默认 4)
    - 等待队列上限 max_waiters (默认 8)
    - 排队等待上限 admission_wait_seconds (默认 5s)
    - 第 max_in_flight + max_waiters + 1 个并发请求立即返回 OVERLOADED
    - draining 状态时新请求返回 SERVER_DRAINING
    """

    def __init__(self, config: RuntimeConfig):
        self._max_in_flight = config.max_in_flight
        self._max_waiters = config.max_waiters
        self._admission_wait = config.admission_wait_seconds
        self._semaphore = asyncio.Semaphore(config.max_in_flight)
        self._waiters = 0
        self._waiters_lock = asyncio.Lock()
        self._draining = False
        # 指标计数
        self._rejected_count = 0
        self._admitted_count = 0

    @property
    def is_draining(self) -> bool:
        return self._draining

    def start_draining(self) -> None:
        """进入 draining 状态，拒绝新请求"""
        self._draining = True
        logger.info("RequestAdmission 进入 draining 状态")

    @property
    def active_count(self) -> int:
        """当前活跃请求数（已获取信号量的）"""
        # Semaphore._value 是剩余可用量
        return self._max_in_flight - self._semaphore._value

    @property
    def waiting_count(self) -> int:
        return self._waiters

    @property
    def rejected_count(self) -> int:
        return self._rejected_count

    @property
    def admitted_count(self) -> int:
        return self._admitted_count

    async def acquire(self) -> None:
        """尝试获取执行槽

        Raises:
            ServiceError(SERVER_DRAINING): 服务正在关闭
            ServiceError(OVERLOADED): 队列已满或等待超时
        """
        if self._draining:
            self._rejected_count += 1
            raise ServiceError(ErrorCode.SERVER_DRAINING)

        async with self._waiters_lock:
            if self._waiters >= self._max_waiters:
                self._rejected_count += 1
                raise ServiceError(ErrorCode.OVERLOADED)
            self._waiters += 1

        try:
            acquired = await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._admission_wait,
            )
            if not acquired:
                self._rejected_count += 1
                raise ServiceError(ErrorCode.OVERLOADED)
            self._admitted_count += 1
        except asyncio.TimeoutError:
            self._rejected_count += 1
            raise ServiceError(ErrorCode.OVERLOADED)
        finally:
            async with self._waiters_lock:
                self._waiters -= 1

    def release(self) -> None:
        """释放执行槽"""
        self._semaphore.release()


# ═══════════════════════════════════════════════════════════
# 运行时控制器（生命周期管理 + 可观测性）
# ═══════════════════════════════════════════════════════════

class RuntimeController:
    """运行时控制器：统一管理准入、draining、指标收集

    在 FastMCP lifespan 中创建和销毁。
    """

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.admission = RequestAdmission(config)
        self._start_time = time.monotonic()
        self._ready = False
        self._init_error: Optional[str] = None
        # 阶段耗时指标（低基数标签）
        self._stage_durations: dict[str, list[float]] = {}
        # 资源池指标
        self._pool_stats: dict[str, dict[str, int]] = {}
        # deadline 超时计数
        self._deadline_exceeded_count = 0
        # tracker 丢弃计数
        self._tracker_dropped_count = 0
        # event-loop lag 采样
        self._event_loop_lag_p99: float = 0.0
        self._lag_samples: list[float] = []
        self._lag_monitor_task: Optional[asyncio.Task] = None

    @property
    def is_ready(self) -> bool:
        """ready 状态：初始化成功且未在 draining"""
        return self._ready and not self.admission.is_draining and self._init_error is None

    @property
    def is_alive(self) -> bool:
        """live 状态：进程存活即可"""
        return True

    def mark_ready(self) -> None:
        self._ready = True
        logger.info("RuntimeController 标记为 ready")

    def mark_init_failed(self, error: str) -> None:
        self._init_error = error
        logger.error(f"RuntimeController 初始化失败: {error}")

    def record_stage_duration(self, stage: str, duration_ms: float) -> None:
        """记录各阶段耗时"""
        if stage not in self._stage_durations:
            self._stage_durations[stage] = []
        self._stage_durations[stage].append(duration_ms)
        # 只保留最近 1000 条
        if len(self._stage_durations[stage]) > 1000:
            self._stage_durations[stage] = self._stage_durations[stage][-500:]

    def record_deadline_exceeded(self) -> None:
        self._deadline_exceeded_count += 1

    def record_tracker_dropped(self) -> None:
        self._tracker_dropped_count += 1

    def update_pool_stats(self, pool_name: str, active: int, waiting: int) -> None:
        self._pool_stats[pool_name] = {"active": active, "waiting": waiting}

    def get_metrics_snapshot(self) -> dict:
        """获取当前指标快照（供 metrics endpoint 使用）"""
        return {
            "active_requests": self.admission.active_count,
            "waiting_requests": self.admission.waiting_count,
            "rejected_requests": self.admission.rejected_count,
            "admitted_requests": self.admission.admitted_count,
            "deadline_exceeded": self._deadline_exceeded_count,
            "tracker_dropped": self._tracker_dropped_count,
            "event_loop_lag_p99_ms": round(self._event_loop_lag_p99, 2),
            "pool_stats": dict(self._pool_stats),
            "stage_durations_avg_ms": {
                k: round(sum(v) / len(v), 2) if v else 0
                for k, v in self._stage_durations.items()
            },
            "uptime_seconds": round(time.monotonic() - self._start_time, 1),
            "draining": self.admission.is_draining,
            "ready": self.is_ready,
        }

    async def start_lag_monitor(self) -> None:
        """启动事件循环延迟监控"""
        self._lag_monitor_task = asyncio.create_task(self._monitor_loop_lag())

    async def _monitor_loop_lag(self) -> None:
        """定期采样事件循环延迟"""
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(0.1)
            lag = (time.monotonic() - t0 - 0.1) * 1000  # ms
            self._lag_samples.append(lag)
            if len(self._lag_samples) > 100:
                self._lag_samples = self._lag_samples[-100:]
                sorted_samples = sorted(self._lag_samples)
                p99_idx = int(len(sorted_samples) * 0.99)
                self._event_loop_lag_p99 = sorted_samples[min(p99_idx, len(sorted_samples) - 1)]

    async def shutdown(self) -> None:
        """关闭运行时控制器"""
        if self._lag_monitor_task:
            self._lag_monitor_task.cancel()
            try:
                await self._lag_monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("RuntimeController 已关闭")
