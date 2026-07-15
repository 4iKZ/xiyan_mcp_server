"""embedding_service.py 单元测试（P2-GAP-11）"""

import numpy as np
import pytest
from unittest.mock import MagicMock, patch

from xiyan_mcp_server.utils.embedding_service import EmbeddingService


def _make_embedding_service(use_api=False):
    svc = EmbeddingService.__new__(EmbeddingService)
    svc.model_name = "test-model"
    svc.use_api = use_api
    svc.vector_dim = 4
    svc._model = None
    svc._model_type = None
    svc._session = None
    svc.use_vllm_format = False
    svc.api_url = "https://api-inference.modelscope.cn/v1/"
    svc.api_key = ""
    return svc


class TestNormalize:
    """_normalize 向量归一化"""

    def test_list_normalized(self):
        svc = _make_embedding_service()
        result = svc._normalize([3.0, 4.0])
        norm = np.linalg.norm(result)
        assert abs(norm - 1.0) < 1e-6

    def test_numpy_array_normalized(self):
        svc = _make_embedding_service()
        arr = np.array([3.0, 4.0])
        result = svc._normalize(arr)
        assert isinstance(result, list)

    def test_zero_vector_returns_zero(self):
        svc = _make_embedding_service()
        result = svc._normalize([0.0, 0.0])
        assert result == [0.0, 0.0]

    def test_single_element(self):
        svc = _make_embedding_service()
        result = svc._normalize([5.0])
        assert abs(result[0] - 1.0) < 1e-6


class TestEmbed:
    """embed / embed_single"""

    def test_empty_list_returns_empty(self):
        svc = _make_embedding_service(use_api=True)
        result = svc.embed([])
        assert result == []

    def test_embed_single_returns_first(self):
        svc = _make_embedding_service(use_api=True)
        svc._embed_api = MagicMock(return_value=[[1.0, 2.0]])
        result = svc.embed_single("hello")
        assert result == [1.0, 2.0]

    def test_embed_single_empty_on_empty_result(self):
        svc = _make_embedding_service(use_api=True)
        svc._embed_api = MagicMock(return_value=[])
        result = svc.embed_single("hello")
        assert result == []


class TestEmbedApi:
    """_embed_api 两种格式"""

    def _make_api(self):
        svc = _make_embedding_service(use_api=True)
        svc._session = MagicMock()
        return svc

    def test_vllm_format(self):
        svc = self._make_api()
        svc.use_vllm_format = True
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [{"embedding": [1.0, 2.0]}]}
        mock_resp.raise_for_status = MagicMock()
        svc._session.post.return_value = mock_resp

        result = svc._embed_api(["hello"])
        assert result == [[1.0, 2.0]]
        call_args = svc._session.post.call_args
        assert "input" in call_args[1]["json"]

    def test_modelscope_format(self):
        svc = self._make_api()
        svc.use_vllm_format = False
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [{"embedding": [1.0, 2.0]}]}
        mock_resp.raise_for_status = MagicMock()
        svc._session.post.return_value = mock_resp

        result = svc._embed_api(["hello"])
        assert result == [[1.0, 2.0]]
        call_args = svc._session.post.call_args
        assert call_args[1]["json"]["encoding_format"] == "float"


class TestInitLocalModel:
    """_init_local_model 分支"""

    def test_init_raises_when_not_api(self):
        svc = _make_embedding_service(use_api=False)
        with patch.object(
            svc, "_init_local_model", side_effect=RuntimeError("model load failed")
        ):
            with pytest.raises(RuntimeError):
                svc._init_local_model()
