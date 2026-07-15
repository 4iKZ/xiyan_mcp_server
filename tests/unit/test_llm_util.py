"""llm_util.py 单元测试（P2-GAP-10）"""

import pytest
from unittest.mock import patch, MagicMock

from xiyan_mcp_server.utils.llm_util import (
    _get_or_create_client,
    _get_or_create_async_client,
)


class TestGetOrCreateClient:
    """同步客户端缓存"""

    def setup_method(self):
        from xiyan_mcp_server.utils import llm_util

        llm_util._client_cache.clear()

    def test_openai_client_cached(self):
        c1 = _get_or_create_client("http://localhost:8000/v1", "key123")
        c2 = _get_or_create_client("http://localhost:8000/v1", "key123")
        assert c1 is c2

    def test_different_keys_different_clients(self):
        c1 = _get_or_create_client("http://localhost:8000/v1", "key1")
        c2 = _get_or_create_client("http://localhost:8000/v1", "key2")
        assert c1 is not c2

    def test_different_urls_different_clients(self):
        c1 = _get_or_create_client("http://a/v1", "key", api_version="v1")
        c2 = _get_or_create_client("http://b/v1", "key", api_version="v1")
        assert c1 is not c2

    def test_azure_client_detected(self):
        c = _get_or_create_client(
            "http://azure.openai.azure.com", "key", api_version="2025-01-01"
        )
        assert type(c).__name__ == "AzureOpenAI"

    def test_openai_client_default(self):
        c = _get_or_create_client("http://localhost:8000/v1", "key")
        assert type(c).__name__ == "OpenAI"


class TestGetOrCreateAsyncClient:
    """异步客户端缓存"""

    def setup_method(self):
        from xiyan_mcp_server.utils import llm_util

        llm_util._async_client_cache.clear()

    def test_async_client_cached(self):
        c1 = _get_or_create_async_client("http://localhost:8000/v1", "key123")
        c2 = _get_or_create_async_client("http://localhost:8000/v1", "key123")
        assert c1 is c2

    def test_async_different_keys_different_clients(self):
        c1 = _get_or_create_async_client("http://localhost:8000/v1", "key1")
        c2 = _get_or_create_async_client("http://localhost:8000/v1", "key2")
        assert c1 is not c2

    def test_async_independent_from_sync_cache(self):
        sync_client = _get_or_create_client("http://localhost:8000/v1", "key")
        async_client = _get_or_create_async_client("http://localhost:8000/v1", "key")
        assert sync_client is not async_client

    def test_async_azure_client_detected(self):
        c = _get_or_create_async_client(
            "http://azure.openai.azure.com", "key", api_version="2025-01-01"
        )
        assert type(c).__name__ == "AsyncAzureOpenAI"
