"""SQL 异步执行管理器

提供受控线程池中的 SQL 执行，支持：
- 并发上限（默认 4）
- 获取执行槽最多等待 5 秒
- 通过 asyncio 异步等待，禁止在事件循环中调用 future.result(timeout=...)
- 超时/取消后等到底层线程真正结束才释放槽位
- 尽可能取消底层查询（psycopg2 connection.cancel()）
"""

import asyncio
import concurrent.futures
import logging
import threading
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from ..runtime import Deadline, DeadlineExceeded

logger = logging.getLogger("xiyan_mcp_server.sql_executor")


@dataclass
class QueryResult:
    """SQL 查询结果"""
    columns: List[str]
    records: List[tuple]
    row_count: int


class SqlExecutionManager:
    """SQL 异步执行管理器

    - ThreadPoolExecutor(max_workers=sql_concurrency)
    - asyncio.Semaphore 控制并发上限
    - 获取执行槽最多等待 slot_wait_seconds
    - 超时/取消后等到底层线程真正结束才释放槽位
    """

    def __init__(
        self,
        sql_concurrency: int = 4,
        slot_wait_seconds: float = 5.0,
        query_timeout_seconds: float = 60.0,
    ):
        self._sql_concurrency = sql_concurrency
        self._slot_wait_seconds = slot_wait_seconds
        self._query_timeout_seconds = query_timeout_seconds

        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=sql_concurrency,
            thread_name_prefix="sql-exec",
        )
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._active_count = 0
        self._active_lock = threading.Lock()
        self._shutdown = False

    def _get_semaphore(self) -> asyncio.Semaphore:
        """延迟创建 Semaphore（必须在事件循环中创建）"""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._sql_concurrency)
        return self._semaphore

    @property
    def active_count(self) -> int:
        with self._active_lock:
            return self._active_count

    @property
    def pool_stats(self) -> dict:
        """返回池状态（供指标使用）"""
        sem = self._semaphore
        waiting = 0
        if sem is not None:
            # _waiters 是 asyncio.Semaphore 内部属性
            waiting = len(sem._waiters) if hasattr(sem, '_waiters') else 0
        return {
            "active": self.active_count,
            "waiting": waiting,
            "max_concurrency": self._sql_concurrency,
        }

    def _execute_sync(self, engine, sql: str, query_timeout: float) -> QueryResult:
        """在工作线程中同步执行 SQL（供线程池 submit 使用）

        尝试在超时时取消底层查询：
        - psycopg2: connection.cancel()
        - 无法确认连接状态时关闭/废弃连接
        """
        conn = None
        raw_conn = None
        try:
            conn = engine.connect()
            # 获取底层 DBAPI 连接（用于取消）
            raw_conn = getattr(
                getattr(conn, 'connection', None), 'dbapi_connection', None
            )

            # 设置语句超时（如果数据库支持）
            self._set_statement_timeout(conn, raw_conn, query_timeout)

            # 使用 sqlalchemy.text 包装 SQL（延迟导入避免硬依赖）
            try:
                from sqlalchemy import text as sa_text
                executable = sa_text(sql)
            except ImportError:
                executable = sql

            cursor = conn.execute(executable)
            columns = list(cursor.keys())
            records = [tuple(row) for row in cursor.fetchall()]
            return QueryResult(columns=columns, records=records, row_count=len(records))

        except Exception as e:
            # 如果是超时相关错误，尝试取消底层查询
            if self._is_timeout_error(e) and raw_conn is not None:
                self._cancel_query(raw_conn)
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _set_statement_timeout(self, conn, raw_conn, timeout: float) -> None:
        """尝试设置语句级超时"""
        try:
            if raw_conn is None:
                return
            dialect_name = getattr(
                getattr(conn, 'engine', None), 'dialect', None
            )
            dialect_name = getattr(dialect_name, 'name', '') if dialect_name else ''
            if dialect_name in ('postgresql', 'greptimedb'):
                try:
                    from sqlalchemy import text as sa_text
                    conn.execute(
                        sa_text(f"SET statement_timeout = '{int(timeout * 1000)}'")
                    )
                except ImportError:
                    pass
        except Exception:
            # 设置失败不影响主流程
            pass

    def _cancel_query(self, raw_conn) -> None:
        """尝试取消底层查询"""
        try:
            # psycopg2 连接支持 cancel()
            if hasattr(raw_conn, 'cancel'):
                raw_conn.cancel()
                logger.info("已发送查询取消请求 (connection.cancel)")
            # sqlite3 连接支持 interrupt()
            elif hasattr(raw_conn, 'interrupt'):
                raw_conn.interrupt()
                logger.info("已发送查询中断请求 (connection.interrupt)")
        except Exception as e:
            logger.warning(f"取消底层查询失败: {e}")

    @staticmethod
    def _is_timeout_error(e: Exception) -> bool:
        """判断是否为超时相关错误"""
        msg = str(e).lower()
        return any(k in msg for k in ['timeout', 'timed out', 'cancel', 'statement_timeout'])

    async def execute_async(
        self,
        engine,
        sql: str,
        *,
        deadline: Deadline,
        max_rows: int = 10000,
    ) -> QueryResult:
        """异步执行 SQL，受 deadline 和并发槽双重控制

        Args:
            engine: SQLAlchemy Engine
            sql: 已预处理的 SQL 语句
            deadline: 绝对截止时间
            max_rows: 最大返回行数

        Returns:
            QueryResult

        Raises:
            DeadlineExceeded: 超时
            asyncio.TimeoutError: 获取执行槽超时
            Exception: SQL 执行错误
        """
        if self._shutdown:
            raise RuntimeError("SqlExecutionManager 已关闭")

        deadline.check()

        # 计算实际查询超时：取 deadline 剩余和默认超时的较小值
        query_timeout = min(deadline.remaining(), self._query_timeout_seconds)
        if query_timeout <= 0:
            raise DeadlineExceeded("请求处理超时")

        sem = self._get_semaphore()

        # 获取执行槽，最多等待 slot_wait_seconds（同时受 deadline 约束）
        slot_timeout = min(self._slot_wait_seconds, deadline.remaining())
        try:
            await asyncio.wait_for(sem.acquire(), timeout=slot_timeout)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"获取 SQL 执行槽超时 ({self._slot_wait_seconds}s)，当前活跃: {self.active_count}"
            )

        # 已获取槽位，执行 SQL
        try:
            with self._active_lock:
                self._active_count += 1

            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                self._executor,
                self._execute_sync,
                engine, sql, query_timeout,
            )

            # 使用 deadline 约束等待时间
            # 注意：不用 asyncio.wait_for（它会取消 future），
            # 改用 asyncio.wait 以便超时后仍能等待线程完成
            remaining = deadline.remaining()
            done, pending = await asyncio.wait({future}, timeout=remaining)

            if pending:
                # 超时：不取消 future（线程会继续执行到底），
                # 但必须等线程真正结束才能释放槽位
                logger.warning(
                    f"SQL 执行超时 ({remaining:.1f}s)，等待线程完成: {sql[:200]}"
                )
                try:
                    await future
                except Exception:
                    pass  # 线程内的错误不再上报
                raise DeadlineExceeded("请求处理超时")

            # 完成：获取结果（可能抛异常）
            result = future.result()

            # 截断结果
            if result.row_count > max_rows:
                logger.warning(
                    f"查询结果超过 {max_rows} 行，已截断。"
                    f"建议使用 LIMIT 或 WHERE 子句减少数据量。"
                )
                result = QueryResult(
                    columns=result.columns,
                    records=result.records[:max_rows],
                    row_count=min(result.row_count, max_rows),
                )

            return result

        finally:
            with self._active_lock:
                self._active_count -= 1
            sem.release()

    async def shutdown(self, wait: bool = True) -> None:
        """关闭执行器"""
        self._shutdown = True
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._executor.shutdown, wait)
        logger.info("SqlExecutionManager 已关闭")


# ═══════════════════════════════════════════════════════════
# 全局单例管理
# ═══════════════════════════════════════════════════════════

_sql_manager: Optional[SqlExecutionManager] = None
_sql_manager_lock = threading.Lock()


def get_sql_manager() -> Optional[SqlExecutionManager]:
    """获取全局 SqlExecutionManager（如果已初始化）"""
    return _sql_manager


def init_sql_manager(
    sql_concurrency: int = 4,
    slot_wait_seconds: float = 5.0,
    query_timeout_seconds: float = 60.0,
) -> SqlExecutionManager:
    """初始化全局 SqlExecutionManager（在 lifespan 中调用）"""
    global _sql_manager
    with _sql_manager_lock:
        if _sql_manager is None:
            _sql_manager = SqlExecutionManager(
                sql_concurrency=sql_concurrency,
                slot_wait_seconds=slot_wait_seconds,
                query_timeout_seconds=query_timeout_seconds,
            )
            logger.info(
                f"SqlExecutionManager 已初始化: "
                f"concurrency={sql_concurrency}, "
                f"slot_wait={slot_wait_seconds}s, "
                f"query_timeout={query_timeout_seconds}s"
            )
    return _sql_manager


async def shutdown_sql_manager() -> None:
    """关闭全局 SqlExecutionManager"""
    global _sql_manager
    with _sql_manager_lock:
        manager = _sql_manager
        _sql_manager = None
    if manager is not None:
        await manager.shutdown(wait=True)
        logger.info("全局 SqlExecutionManager 已关闭")
