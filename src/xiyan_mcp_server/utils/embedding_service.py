"""
Embedding 服务模块

支持 ModelScope 本地模型和 API 两种模式，用于文本向量化。
"""
import logging
from typing import List, Optional
import numpy as np
import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)


class EmbeddingService:
    """
    Embedding 模型封装类

    支持两种模式：
    1. 本地模型：使用 sentence-transformers 加载模型
    2. 本地 vLLM API (use_vllm_format=True)：调用本地 vLLM 服务
    3. 云端 ModelScope API：自动添加 encoding_format 参数

    API 模式自动检测：
    - use_vllm_format=True → 使用本地 vLLM 格式（texts 字段）
    - api_url 包含 "modelscope.cn" → 自动添加 encoding_format="float"
    """
    
    def __init__(self, config: dict):
        """
        初始化 Embedding 服务

        Args:
            config: 配置字典，包含：
                - model: 模型名称
                - use_api: 是否使用 API 模式
                - api_key: API 密钥（API 模式需要）
                - api_url: API 地址（可选，默认为 ModelScope）
                - vector_dim: 向量维度
                - use_vllm_format: 是否使用 vLLM 格式（请求用 texts，响应用 embeddings）
        """
        self.model_name = config.get("model", "iic/nlp_gte_sentence-embedding_chinese-base")
        self.use_api = config.get("use_api", False)
        self.api_key = config.get("api_key", "")
        self.api_url = config.get("api_url", "https://api-inference.modelscope.cn/v1/")
        self.use_vllm_format = config.get("use_vllm_format", False)
        self.vector_dim = config.get("vector_dim", 768)
        self._model = None

        if not self.use_api:
            self._init_local_model()
        else:
            # API 模式：复用 HTTP Session（复用 TCP 连接池，避免每次调用重建连接）
            self._session = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8)
            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)
    
    def _init_local_model(self):
        """初始化本地模型"""
        try:
            from sentence_transformers import SentenceTransformer
            
            # 尝试从 ModelScope 加载
            logger.info(f"正在加载 Embedding 模型: {self.model_name}")
            
            # 对于 ModelScope 模型，需要特殊处理
            if self.model_name.startswith("iic/") or self.model_name.startswith("damo/"):
                # ModelScope 模型
                try:
                    from modelscope.models import Model
                    from modelscope.pipelines import pipeline
                    
                    self._model = pipeline(
                        'sentence-embedding',
                        model=self.model_name,
                        model_revision='v1.0.0'
                    )
                    self._model_type = 'modelscope'
                    logger.info("使用 ModelScope pipeline 加载模型")
                except Exception as e:
                    logger.warning(f"ModelScope 加载失败，尝试回退到 sentence-transformers: {e}")
                    # 尝试用 sentence-transformers
                    self._model = SentenceTransformer(self.model_name)
                    self._model_type = 'sentence_transformers'
                    logger.info("使用 sentence-transformers 加载模型")
            else:
                # HuggingFace 模型，直接使用 sentence-transformers
                self._model = SentenceTransformer(self.model_name)
                self._model_type = 'sentence_transformers'
                logger.info("使用 sentence-transformers 加载模型")
                
            logger.info(f"Embedding 模型加载成功，向量维度: {self.vector_dim}")
            
        except Exception as e:
            logger.error(f"加载 Embedding 模型失败: {e}")
            raise
    
    def embed(self, texts: List[str]) -> List[List[float]]:
        """
        批量生成文本向量
        
        Args:
            texts: 文本列表
            
        Returns:
            向量列表，每个向量是 float 列表
        """
        if not texts:
            return []
        
        if self.use_api:
            return self._embed_api(texts)
        else:
            return self._embed_local(texts)
    
    def embed_single(self, text: str) -> List[float]:
        """
        单条文本向量化
        
        Args:
            text: 输入文本
            
        Returns:
            向量（float 列表）
        """
        result = self.embed([text])
        return result[0] if result else []
    
    def _embed_local(self, texts: List[str]) -> List[List[float]]:
        """使用本地模型进行向量化"""
        if self._model is None:
            raise RuntimeError("Embedding 模型未初始化")
        
        try:
            if self._model_type == 'modelscope':
                # ModelScope pipeline
                results = []
                for text in texts:
                    output = self._model(input=text)
                    # 输出格式可能是 {'text_embedding': [...]}
                    if isinstance(output, dict) and 'text_embedding' in output:
                        vec = output['text_embedding']
                    else:
                        vec = output
                    results.append(self._normalize(vec))
                return results
            else:
                # sentence-transformers
                embeddings = self._model.encode(texts, normalize_embeddings=True)
                return embeddings.tolist()
                
        except Exception as e:
            logger.error(f"本地向量化失败: {e}")
            raise
    
    def _embed_api(self, texts: List[str]) -> List[List[float]]:
        """使用 API 进行向量化

        支持两种 API 格式：
        1. 本地 vLLM (use_vllm_format=True)
        2. ModelScope 云端 API（自动添加 encoding_format 参数）
        """
        try:
            if self.use_vllm_format:
                # 本地 vLLM 使用 OpenAI 兼容格式：input 字段
                response = self._session.post(
                    f"{self.api_url}embeddings",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}"
                    },
                    json={
                        "model": self.model_name,
                        "input": texts
                    },
                    timeout=60
                )
                response.raise_for_status()
                result = response.json()
                # OpenAI 响应格式: {"data": [{"embedding": [...]}, ...]}
                embeddings = [item["embedding"] for item in result.get("data", [])]
                return embeddings
            else:
                # ModelScope 云端 API：需要 encoding_format 参数
                response = self._session.post(
                    f"{self.api_url}embeddings",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}"
                    },
                    json={
                        "model": self.model_name,
                        "input": texts,
                        "encoding_format": "float"  # ModelScope 要求此参数
                    },
                    timeout=60
                )
                response.raise_for_status()
                result = response.json()
                # ModelScope 响应格式: {"data": [{"embedding": [...]}]}
                embeddings = [item["embedding"] for item in result.get("data", [])]
                return embeddings

        except Exception as e:
            logger.error(f"API 向量化失败: {e}")
            raise
    
    def _normalize(self, vec) -> List[float]:
        """归一化向量"""
        if isinstance(vec, list):
            vec = np.array(vec)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()
    
    @property
    def dimension(self) -> int:
        """返回向量维度"""
        return self.vector_dim
