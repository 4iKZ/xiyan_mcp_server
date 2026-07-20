"""Wave 1B: LLM Gateway 分组限流 + 异步 Embedding 测试

验证：
- 主 LLM 与 Stage2 共享同一分组时，总活跃请求不超过 2
- 不同 base_url 自动分为不同组
- deadline 传播正确
- 同步脚本接口仍可运行（保留兼容）
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xiyan_mcp_server.runtime import Deadline, DeadlineExceeded
from xiyan_mcp_server.utils.llm_gateway import (
    AsyncLLMGateway,
    LLMGroupLimiter,
    _normalize_base_url,
    init_llm_gateway,
    get_llm_gateway,
    shutdown_llm_gateway,
)


# ═══════════════════════════════════════════════════════════
# LLMGroupLimiter 测试
# ═══════════════════════════════════════════════════════════

class TestLLMGroupLimiter:
    @pytest.mark.asyncio
    async def test_basic_acquire_release(self):
        limiter = LLMGroupLimiter(max_concurrency=2)
        deadline = Deadline.after(5)

        await limiter.acquire(deadline)
        assert limiter.active == 1
        limiter.release()
        assert limiter.active == 0

    @pytest.mark.asyncio
    async def test_concurrency_limited_to_2(self):
        """验证分组并发上限为 2"""
        limiter = LLMGroupLimiter(max_concurrency=2)
        deadline = Deadline.after(5)
        peak = 0

        async def worker():
            nonlocal peak
            await limiter.acquire(deadline)
            current = limiter.active
            if current > peak:
                peak = current
            await asyncio.sleep(0.05)
            limiter.release()

        await asyncio.gather(*[worker() for _ in range(6)])
        assert peak <= 2

    @pytest.mark.asyncio
    async def test_deadline_exceeded_on_acquire(self):
        """deadline 过期时获取槽位抛出 DeadlineExceeded"""
        limiter = LLMGroupLimiter(max_concurrency=1)
        deadline = Deadline.after(5)

        # 占满槽位
        await limiter.acquire(deadline)

        # 新请求用极短 deadline
        short_deadline = Deadline.after(0.05)
        with pytest.raises(DeadlineExceeded):
            await limiter.acquire(short_deadline)

        limiter.release()


# ═══════════════════════════════════════════════════════════
# AsyncLLMGateway 测试
# ═══════════════════════════════════════════════════════════

class TestAsyncLLMGateway:
    def test_normalize_base_url(self):
        assert _normalize_base_url("http://localhost:8001/v1/") == "http://localhost:8001/v1"
        assert _normalize_base_url("http://10.0.0.1:8002/v1/?key=x") == "http://10.0.0.1:8002/v1"

    @pytest.mark.asyncio
    async def test_same_group_shares_concurrency(self):
        """主 LLM 与 Stage2 共享同一分组时，总活跃请求不超过 2"""
        gateway = AsyncLLMGateway(default_group_concurrency=2)
        deadline = Deadline.after(10)
        peak = 0
        lock = asyncio.Lock()

        # Mock 客户端
        mock_client = AsyncMock()

        async def slow_create(**kwargs):
            nonlocal peak
            async with lock:
                group_stats = gateway.get_group_stats()
                for g in group_stats.values():
                    if g["active"] > peak:
                        peak = g["active"]
            await asyncio.sleep(0.1)
            return MagicMock()

        mock_client.chat.completions.create = slow_create
        mock_client.close = AsyncMock()

        # 直接 mock _get_or_create_client 避免 openai 依赖
        gateway._get_or_create_client = lambda *a, **kw: mock_client

        # 4 个并发调用同一 base_url
        tasks = [
            gateway.chat_completion(
                base_url="http://localhost:8001/v1/",
                api_key="key1",
                model="test",
                messages=[{"role": "user", "content": "hi"}],
                deadline=deadline,
            )
            for _ in range(4)
        ]
        await asyncio.gather(*tasks)

        assert peak <= 2, f"峰值活跃 {peak}，应 <= 2"
        await gateway.close()

    @pytest.mark.asyncio
    async def test_different_urls_different_groups(self):
        """不同 base_url 自动分为不同组"""
        gateway = AsyncLLMGateway(default_group_concurrency=1)

        mock_client1 = AsyncMock()
        mock_client1.chat.completions.create = AsyncMock(return_value=MagicMock())
        mock_client1.close = AsyncMock()

        mock_client2 = AsyncMock()
        mock_client2.chat.completions.create = AsyncMock(return_value=MagicMock())
        mock_client2.close = AsyncMock()

        # 根据 base_url 返回不同的 mock 客户端
        def mock_get_client(base_url, api_key, api_version=None):
            if "a:8001" in base_url:
                return mock_client1
            return mock_client2

        gateway._get_or_create_client = mock_get_client

        deadline = Deadline.after(5)

        # 两个不同组的调用应该能同时进行
        t0 = time.monotonic()
        await asyncio.gather(
            gateway.chat_completion(
                base_url="http://a:8001/v1/",
                api_key="k1",
                model="m1",
                messages=[],
                deadline=deadline,
            ),
            gateway.chat_completion(
                base_url="http://b:8002/v1/",
                api_key="k2",
                model="m2",
                messages=[],
                deadline=deadline,
            ),
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0

        stats = gateway.get_group_stats()
        assert len(stats) == 2
        await gateway.close()

    @pytest.mark.asyncio
    async def test_explicit_group_override(self):
        """显式 group 参数覆盖默认分组"""
        gateway = AsyncLLMGateway(default_group_concurrency=1)
        deadline = Deadline.after(5)

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=MagicMock())
        mock_client.close = AsyncMock()
        gateway._get_or_create_client = lambda *a, **kw: mock_client

        # 使用显式 group
        await gateway.chat_completion(
            base_url="http://a:8001/v1/",
            api_key="k",
            model="m",
            messages=[],
            deadline=deadline,
            group="custom_group",
        )

        stats = gateway.get_group_stats()
        assert "custom_group" in stats
        await gateway.close()

    @pytest.mark.asyncio
    async def test_deadline_exceeded_propagates(self):
        """deadline 过期时正确传播"""
        gateway = AsyncLLMGateway(default_group_concurrency=2)
        deadline = Deadline.after(0.001)
        await asyncio.sleep(0.01)  # 让 deadline 过期

        with pytest.raises(DeadlineExceeded):
            await gateway.chat_completion(
                base_url="http://x:8001/v1/",
                api_key="k",
                model="m",
                messages=[],
                deadline=deadline,
            )
        await gateway.close()

    @pytest.mark.asyncio
    async def test_closed_gateway_raises(self):
        """关闭后的 gateway 拒绝新请求"""
        gateway = AsyncLLMGateway()
        await gateway.close()

        deadline = Deadline.after(5)
        with pytest.raises(RuntimeError, match="已关闭"):
            await gateway.chat_completion(
                base_url="http://x:8001/v1/",
                api_key="k",
                model="m",
                messages=[],
                deadline=deadline,
            )


# ═══════════════════════════════════════════════════════════
# 全局单例测试
# ═══════════════════════════════════════════════════════════

class TestLLMGatewaySingleton:
    @pytest.mark.asyncio
    async def test_init_and_shutdown(self):
        gw = init_llm_gateway(default_group_concurrency=3)
        assert get_llm_gateway() is gw
        await shutdown_llm_gateway()
        assert get_llm_gateway() is None


# ═══════════════════════════════════════════════════════════
# AsyncEmbeddingService 测试（mock httpx）
# ═══════════════════════════════════════════════════════════

class TestAsyncEmbeddingService:
    @pytest.mark.asyncio
    async def test_api_mode_embed(self):
        """API 模式异步向量化"""
        from xiyan_mcp_server.utils.embedding_async import AsyncEmbeddingService

        config = {
            "model": "test-model",
            "use_api": True,
            "api_key": "test-key",
            "api_url": "http://localhost:9999/v1/",
            "use_vllm_format": True,
            "vector_dim": 128,
        }
        service = AsyncEmbeddingService(config, schema_metadata_concurrency=2)

        # Mock httpx client
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [{"embedding": [0.1] * 128}, {"embedding": [0.2] * 128}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.aclose = AsyncMock()

        service._http_client = mock_http
        service._semaphore = asyncio.Semaphore(2)
        service._started = True

        deadline = Deadline.after(5)
        result = await service.embed_async(["hello", "world"], deadline=deadline)

        assert len(result) == 2
        assert len(result[0]) == 128
        mock_http.post.assert_called_once()
        await service.close()

    @pytest.mark.asyncio
    async def test_deadline_exceeded(self):
        """deadline 过期时抛出 DeadlineExceeded"""
        from xiyan_mcp_server.utils.embedding_async import AsyncEmbeddingService

        config = {"model": "m", "use_api": True, "api_url": "http://x/v1/", "vector_dim": 64}
        service = AsyncEmbeddingService(config)
        service._semaphore = asyncio.Semaphore(2)
        service._started = True
        service._http_client = AsyncMock()

        deadline = Deadline.after(0.0)
        await asyncio.sleep(0.01)

        with pytest.raises(DeadlineExceeded):
            await service.embed_async(["test"], deadline=deadline)

    @pytest.mark.asyncio
    async def test_empty_texts_returns_empty(self):
        """空文本列表返回空结果"""
        from xiyan_mcp_server.utils.embedding_async import AsyncEmbeddingService

        config = {"model": "m", "use_api": True, "vector_dim": 64}
        service = AsyncEmbeddingService(config)
        service._started = True
        service._semaphore = asyncio.Semaphore(2)

        deadline = Deadline.after(5)
        result = await service.embed_async([], deadline=deadline)
        assert result == []
