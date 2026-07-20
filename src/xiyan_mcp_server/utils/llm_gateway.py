"""统一异步 LLM Gateway

提供：
- 按 concurrency_group 分组的并发限流（默认以规范化 base_url 为分组键）
- 每个分组默认并发上限 2
- 所有调用使用 deadline.remaining() 设置实际超时
- 客户端在 lifespan 中创建和关闭，不允许 import 阶段创建
- 保留同步接口供离线脚本使用
"""

import asyncio
import hashlib
import logging
import re
import threading
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from ..runtime import Deadline, DeadlineExceeded

logger = logging.getLogger("xiyan_mcp_server.llm_gateway")


def _normalize_base_url(base_url: str) -> str:
    """规范化 base_url 作为默认分组键"""
    parsed = urlparse(base_url)
    # 只保留 scheme + host + port + path（去掉 query/fragment）
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


class LLMGroupLimiter:
    """单个 LLM 分组的并发限流器"""

    def __init__(self, max_concurrency: int = 2):
        self._max_concurrency = max_concurrency
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._active = 0

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    @property
    def active(self) -> int:
        return self._active

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    async def acquire(self, deadline: Deadline) -> None:
        """获取 LLM 调用槽位，受 deadline 约束"""
        sem = self._get_semaphore()
        remaining = deadline.remaining()
        if remaining <= 0:
            raise DeadlineExceeded("请求处理超时")
        try:
            await asyncio.wait_for(sem.acquire(), timeout=remaining)
        except asyncio.TimeoutError:
            raise DeadlineExceeded("请求处理超时")
        self._active += 1

    def release(self) -> None:
        self._active -= 1
        if self._semaphore is not None:
            self._semaphore.release()


class AsyncLLMGateway:
    """统一异步 LLM Gateway

    - 按 concurrency_group 分组限流
    - 未显式配置时以规范化 base_url 作为分组键
    - 每个分组默认并发上限 2
    - 客户端在 lifespan 中创建和关闭
    """

    def __init__(self, default_group_concurrency: int = 2):
        self._default_concurrency = default_group_concurrency
        self._group_limiters: Dict[str, LLMGroupLimiter] = {}
        self._clients: Dict[str, Any] = {}  # cache_key -> AsyncOpenAI client
        self._lock = threading.Lock()
        self._closed = False

    def _get_group_key(self, base_url: str, group: Optional[str] = None) -> str:
        """获取分组键"""
        if group:
            return group
        return _normalize_base_url(base_url)

    def _get_limiter(self, group_key: str) -> LLMGroupLimiter:
        """获取或创建分组限流器"""
        if group_key not in self._group_limiters:
            with self._lock:
                if group_key not in self._group_limiters:
                    self._group_limiters[group_key] = LLMGroupLimiter(
                        self._default_concurrency
                    )
        return self._group_limiters[group_key]

    def _get_or_create_client(self, base_url: str, api_key: str, api_version: Optional[str] = None):
        """获取或创建 AsyncOpenAI 客户端（按 base_url + key 缓存）"""
        from openai import AsyncOpenAI, AsyncAzureOpenAI

        key_hash = hashlib.md5(api_key.encode()).hexdigest()[:8]
        cache_key = f"{base_url}:{key_hash}:{api_version or ''}"

        if cache_key not in self._clients:
            with self._lock:
                if cache_key not in self._clients:
                    if "azure" in base_url:
                        client = AsyncAzureOpenAI(
                            api_version=api_version or "2025-01-01-preview",
                            api_key=api_key,
                            azure_endpoint=base_url,
                            timeout=120.0,
                        )
                    else:
                        client = AsyncOpenAI(
                            api_key=api_key,
                            base_url=base_url,
                            timeout=120.0,
                        )
                    self._clients[cache_key] = client
                    logger.info(f"LLM Gateway 创建客户端: {base_url}")

        return self._clients[cache_key]

    async def chat_completion(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list,
        deadline: Deadline,
        group: Optional[str] = None,
        temperature: float = 0.0,
        api_version: Optional[str] = None,
        **kwargs,
    ) -> Any:
        """异步 LLM 调用，受分组限流和 deadline 约束

        Args:
            base_url: API 地址
            api_key: API key
            model: 模型名称
            messages: 消息列表
            deadline: 绝对截止时间
            group: 显式分组名（可选，默认用 base_url）
            temperature: 采样温度
            api_version: Azure API 版本（可选）
            **kwargs: 传递给 chat.completions.create 的额外参数

        Returns:
            Completion 响应对象

        Raises:
            DeadlineExceeded: 超时
        """
        if self._closed:
            raise RuntimeError("LLM Gateway 已关闭")

        group_key = self._get_group_key(base_url, group)
        limiter = self._get_limiter(group_key)

        # 获取分组槽位
        await limiter.acquire(deadline)
        try:
            deadline.check()

            client = self._get_or_create_client(base_url, api_key, api_version)

            # 使用 deadline 剩余时间作为请求超时
            remaining = deadline.remaining()
            if remaining <= 0:
                raise DeadlineExceeded("请求处理超时")

            # 设置请求级超时
            create_kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "timeout": remaining,
                **kwargs,
            }

            try:
                completion = await client.chat.completions.create(**create_kwargs)
                return completion
            except Exception as e:
                # 将 httpx/openai 超时错误映射为 DeadlineExceeded
                if self._is_timeout_error(e):
                    raise DeadlineExceeded("请求处理超时")
                raise
        finally:
            limiter.release()

    @staticmethod
    def _is_timeout_error(e: Exception) -> bool:
        """判断是否为超时相关错误"""
        msg = str(e).lower()
        type_name = type(e).__name__.lower()
        return any(k in msg or k in type_name for k in [
            'timeout', 'timed out', 'timeouterror', 'readtimeout',
            'connecttimeout', 'pooltimeout',
        ])

    def get_group_stats(self) -> Dict[str, Dict[str, int]]:
        """获取各分组的并发状态"""
        return {
            key: {"active": limiter.active, "max": limiter.max_concurrency}
            for key, limiter in self._group_limiters.items()
        }

    async def close(self) -> None:
        """关闭所有客户端连接"""
        self._closed = True
        for cache_key, client in self._clients.items():
            try:
                await client.close()
            except Exception as e:
                logger.warning(f"关闭 LLM 客户端失败 ({cache_key}): {e}")
        self._clients.clear()
        logger.info("LLM Gateway 已关闭")


# ═══════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════

_llm_gateway: Optional[AsyncLLMGateway] = None
_llm_gateway_lock = threading.Lock()


def get_llm_gateway() -> Optional[AsyncLLMGateway]:
    """获取全局 LLM Gateway（如果已初始化）"""
    return _llm_gateway


def init_llm_gateway(default_group_concurrency: int = 2) -> AsyncLLMGateway:
    """初始化全局 LLM Gateway（在 lifespan 中调用）"""
    global _llm_gateway
    with _llm_gateway_lock:
        if _llm_gateway is None:
            _llm_gateway = AsyncLLMGateway(
                default_group_concurrency=default_group_concurrency
            )
            logger.info(
                f"LLM Gateway 已初始化: default_group_concurrency={default_group_concurrency}"
            )
    return _llm_gateway


async def shutdown_llm_gateway() -> None:
    """关闭全局 LLM Gateway"""
    global _llm_gateway
    with _llm_gateway_lock:
        gateway = _llm_gateway
        _llm_gateway = None
    if gateway is not None:
        await gateway.close()
        logger.info("全局 LLM Gateway 已关闭")
