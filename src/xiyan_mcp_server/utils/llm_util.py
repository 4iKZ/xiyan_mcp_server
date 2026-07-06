import hashlib
import logging
import threading

from openai import AzureOpenAI, OpenAI

logger = logging.getLogger(__name__)

# 客户端缓存：(client_type, base_url, key_hash) -> client
_client_cache = {}
_client_cache_lock = threading.Lock()


def _get_or_create_client(base_url: str, key: str, api_version: str = None):
    """获取或创建 OpenAI 客户端（线程安全，按 base_url + key + api_version 缓存）

    复用客户端 = 复用底层 httpx 连接池，避免每次调用都重建 TLS 握手。
    """
    client_type = "azure" if "azure" in base_url else "openai"
    key_hash = hashlib.md5(key.encode()).hexdigest()[:8]
    cache_key = (client_type, base_url, key_hash, api_version)

    if cache_key not in _client_cache:
        with _client_cache_lock:
            if cache_key not in _client_cache:
                if client_type == "azure":
                    client = AzureOpenAI(
                        api_version=api_version or "2025-01-01-preview",
                        api_key=key,
                        azure_endpoint=base_url,
                        azure_deployment=None,  # deployment 在 chat.completions.create 时指定
                        timeout=120.0,
                    )
                    logger.info(f"创建 AzureOpenAI 客户端: endpoint={base_url}, api_version={api_version}")
                else:
                    client = OpenAI(
                        api_key=key,
                        base_url=base_url,
                        timeout=120.0,
                    )
                    logger.info(f"创建 OpenAI 客户端: base_url={base_url}")
                _client_cache[cache_key] = client

    return _client_cache[cache_key]


def call_openai_sdk(**args):
    key = args["key"]
    base_url = args["url"]
    model = args.get("model", "gpt-3.5-turbo")

    api_version = args.get("api_version", "2025-01-01-preview")
    client = _get_or_create_client(base_url, key, api_version)

    del args["key"]
    del args["url"]
    args.pop("api_version", None)

    logging.debug(f"Calling LLM chat completions with model: {model}")
    completion = client.chat.completions.create(**args)
    return completion
