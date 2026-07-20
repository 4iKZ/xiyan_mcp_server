"""异步 Embedding 服务

提供：
- API Embedding 使用生命周期管理的 httpx 异步 HTTP 客户端
- 本地 Embedding 放入独立受控线程池
- 保留同步接口供离线索引、脚本和评测工具使用
- 所有调用使用 deadline.remaining() 设置实际超时
"""

import asyncio
import logging
from typing import List, Optional

from ..runtime import Deadline, DeadlineExceeded

logger = logging.getLogger("xiyan_mcp_server.embedding_async")


class AsyncEmbeddingService:
    """异步 Embedding 服务包装器

    包装现有 EmbeddingService，提供异步接口：
    - API 模式：使用 httpx.AsyncClient 直接异步调用
    - 本地模式：通过 asyncio.to_thread 放入线程池
    """

    def __init__(
        self,
        config: dict,
        schema_metadata_concurrency: int = 2,
    ):
        self._config = config
        self._model_name = config.get("model", "")
        self._use_api = config.get("use_api", False)
        self._api_key = config.get("api_key", "")
        self._api_url = config.get("api_url", "https://api-inference.modelscope.cn/v1/")
        self._use_vllm_format = config.get("use_vllm_format", False)
        self._vector_dim = config.get("vector_dim", 768)

        self._http_client = None  # httpx.AsyncClient，在 start() 中创建
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._schema_concurrency = schema_metadata_concurrency
        self._sync_service = None  # 延迟初始化的同步服务（本地模式用）
        self._started = False

    async def start(self) -> None:
        """启动服务（在 lifespan 中调用）"""
        if self._started:
            return

        if self._use_api:
            import httpx
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=10.0),
                limits=httpx.Limits(
                    max_connections=8,
                    max_keepalive_connections=4,
                ),
            )
            logger.info(f"AsyncEmbeddingService 启动 (API 模式): {self._api_url}")
        else:
            # 本地模式：初始化同步服务
            from .embedding_service import EmbeddingService
            self._sync_service = EmbeddingService(self._config)
            logger.info("AsyncEmbeddingService 启动 (本地模式)")

        self._semaphore = asyncio.Semaphore(self._schema_concurrency)
        self._started = True

    async def embed_async(
        self,
        texts: List[str],
        *,
        deadline: Deadline,
    ) -> List[List[float]]:
        """异步批量生成文本向量

        Args:
            texts: 文本列表
            deadline: 绝对截止时间

        Returns:
            向量列表
        """
        if not texts:
            return []

        if not self._started:
            raise RuntimeError("AsyncEmbeddingService 未启动，请先调用 start()")

        deadline.check()

        # 受 schema_metadata 并发限制
        sem = self._semaphore
        remaining = deadline.remaining()
        if remaining <= 0:
            raise DeadlineExceeded("请求处理超时")

        try:
            await asyncio.wait_for(sem.acquire(), timeout=remaining)
        except asyncio.TimeoutError:
            raise DeadlineExceeded("请求处理超时")

        try:
            if self._use_api:
                return await self._embed_api_async(texts, deadline)
            else:
                return await self._embed_local_async(texts, deadline)
        finally:
            sem.release()

    async def embed_single_async(
        self,
        text: str,
        *,
        deadline: Deadline,
    ) -> List[float]:
        """异步单条文本向量化"""
        result = await self.embed_async([text], deadline=deadline)
        return result[0] if result else []

    async def _embed_api_async(
        self,
        texts: List[str],
        deadline: Deadline,
    ) -> List[List[float]]:
        """使用 httpx 异步客户端调用 API"""
        remaining = deadline.remaining()
        if remaining <= 0:
            raise DeadlineExceeded("请求处理超时")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        payload = {
            "model": self._model_name,
            "input": texts,
        }
        # ModelScope 云端需要 encoding_format 参数
        if not self._use_vllm_format and "modelscope" in self._api_url:
            payload["encoding_format"] = "float"

        try:
            response = await self._http_client.post(
                f"{self._api_url}embeddings",
                headers=headers,
                json=payload,
                timeout=remaining,
            )
            response.raise_for_status()
            result = response.json()
            embeddings = [item["embedding"] for item in result.get("data", [])]
            return embeddings
        except Exception as e:
            if self._is_timeout_error(e):
                raise DeadlineExceeded("请求处理超时")
            logger.error(f"API 异步向量化失败: {e}")
            raise

    async def _embed_local_async(
        self,
        texts: List[str],
        deadline: Deadline,
    ) -> List[List[float]]:
        """本地模型通过线程池异步执行"""
        remaining = deadline.remaining()
        if remaining <= 0:
            raise DeadlineExceeded("请求处理超时")

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._sync_service.embed, texts),
                timeout=remaining,
            )
            return result
        except asyncio.TimeoutError:
            raise DeadlineExceeded("请求处理超时")

    @staticmethod
    def _is_timeout_error(e: Exception) -> bool:
        msg = str(e).lower()
        type_name = type(e).__name__.lower()
        return any(k in msg or k in type_name for k in [
            'timeout', 'timed out', 'timeouterror', 'readtimeout',
        ])

    @property
    def dimension(self) -> int:
        return self._vector_dim

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        self._started = False
        logger.info("AsyncEmbeddingService 已关闭")


# ═══════════════════════════════════════════════════════════
# 异步 Redis 客户端包装
# ═══════════════════════════════════════════════════════════

class AsyncRedisClient:
    """异步 Redis 客户端包装器

    使用 redis.asyncio 原生异步客户端，
    所有操作使用 deadline.remaining() 设置超时。
    """

    def __init__(self, host: str, port: int, password: str = ""):
        self._host = host
        self._port = port
        self._password = password
        self._client = None

    async def start(self) -> None:
        """创建异步 Redis 连接"""
        import redis.asyncio as aioredis
        self._client = aioredis.Redis(
            host=self._host,
            port=self._port,
            password=self._password or None,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
        # 测试连接
        await self._client.ping()
        logger.info(f"AsyncRedisClient 已连接: {self._host}:{self._port}")

    @property
    def client(self):
        """获取底层 redis.asyncio.Redis 客户端"""
        if self._client is None:
            raise RuntimeError("AsyncRedisClient 未启动")
        return self._client

    async def search(
        self,
        index_name: str,
        query: str,
        *,
        deadline: Deadline,
        **kwargs,
    ):
        """执行 Redis 搜索，受 deadline 约束"""
        remaining = deadline.remaining()
        if remaining <= 0:
            raise DeadlineExceeded("请求处理超时")

        try:
            return await asyncio.wait_for(
                self._client.ft(index_name).search(query, **kwargs),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            raise DeadlineExceeded("请求处理超时")

    async def close(self) -> None:
        """关闭连接"""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("AsyncRedisClient 已关闭")


# ═══════════════════════════════════════════════════════════
# 全局单例管理
# ═══════════════════════════════════════════════════════════

import threading

_async_embedding: Optional[AsyncEmbeddingService] = None
_async_embedding_lock = threading.Lock()


def get_async_embedding() -> Optional[AsyncEmbeddingService]:
    return _async_embedding


def init_async_embedding(config: dict, schema_metadata_concurrency: int = 2) -> AsyncEmbeddingService:
    global _async_embedding
    with _async_embedding_lock:
        if _async_embedding is None:
            _async_embedding = AsyncEmbeddingService(
                config, schema_metadata_concurrency=schema_metadata_concurrency
            )
    return _async_embedding


async def shutdown_async_embedding() -> None:
    global _async_embedding
    with _async_embedding_lock:
        service = _async_embedding
        _async_embedding = None
    if service is not None:
        await service.close()
