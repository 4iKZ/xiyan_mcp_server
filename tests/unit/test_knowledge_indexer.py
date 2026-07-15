"""knowledge_indexer.py 单元测试（P0-GAP-04）"""

import json
import pytest
from unittest.mock import MagicMock, patch

from xiyan_mcp_server.utils.knowledge_indexer import KnowledgeIndexer


@pytest.fixture
def indexer():
    redis = MagicMock()
    embed = MagicMock()
    embed.embed.return_value = [[0.1, 0.2, 0.3]] * 3
    cfg = {
        "index_name": "test_idx",
        "vector_dim": 3,
        "knowledge_dir": "json",
    }
    return KnowledgeIndexer(redis, embed, cfg)


class TestLoadKnowledge:
    """load_knowledge"""

    def test_empty_dir_returns_empty(self, tmp_path, indexer):
        indexer.knowledge_dir = str(tmp_path)
        result = indexer.load_knowledge()
        assert result == []

    def test_loads_knowledge_files(self, tmp_path, indexer):
        kb_dir = tmp_path / "json"
        kb_dir.mkdir()
        data = [
            {"table_name": "t1", "description": "d1"},
            {"table_name": "t2", "description": "d2"},
        ]
        (kb_dir / "db_a_knowledge.json").write_text(json.dumps(data))
        indexer.knowledge_dir = str(kb_dir)
        result = indexer.load_knowledge()
        assert len(result) == 2
        assert all(item["database"] == "db_a" for item in result)

    def test_invalid_json_skipped(self, tmp_path, indexer):
        kb_dir = tmp_path / "json"
        kb_dir.mkdir()
        (kb_dir / "bad_knowledge.json").write_text("not json")
        indexer.knowledge_dir = str(kb_dir)
        result = indexer.load_knowledge()
        assert result == []

    def test_multiple_files_loaded(self, tmp_path, indexer):
        kb_dir = tmp_path / "json"
        kb_dir.mkdir()
        (kb_dir / "db_a_knowledge.json").write_text(json.dumps([{"table_name": "t1"}]))
        (kb_dir / "db_b_knowledge.json").write_text(json.dumps([{"table_name": "t2"}]))
        indexer.knowledge_dir = str(kb_dir)
        result = indexer.load_knowledge()
        dbs = {item["database"] for item in result}
        assert dbs == {"db_a", "db_b"}


class TestCreateIndex:
    """create_index"""

    def test_creates_index(self, indexer):
        indexer.redis.ft.return_value.info.side_effect = Exception("not found")
        indexer.create_index()
        assert indexer.redis.ft.call_count == 2
        indexer.redis.ft.return_value.create_index.assert_called_once()

    def test_drop_existing_drops_then_creates(self, indexer):
        indexer.redis.ft.return_value.info.return_value = {}
        with patch.object(indexer, "drop_index") as mock_drop:
            indexer.create_index(drop_existing=True)
            mock_drop.assert_called_once()
            indexer.redis.ft.return_value.create_index.assert_called_once()

    def test_skip_if_exists(self, indexer):
        indexer.redis.ft.return_value.info.return_value = {}
        indexer.create_index()
        indexer.redis.ft.return_value.create_index.assert_not_called()


class TestIndexKnowledge:
    """index_knowledge"""

    def test_empty_list_returns_early(self, indexer):
        indexer.index_knowledge([])
        indexer.redis.pipeline.assert_not_called()

    def test_indexes_items(self, indexer):
        knowledge = [
            {"table_name": "t1", "database": "db1", "description": "d1"},
            {"table_name": "t2", "database": "db1", "description": "d2"},
        ]
        indexer.index_knowledge(knowledge)
        indexer.redis.pipeline.assert_called_once()
        pipeline = indexer.redis.pipeline.return_value
        assert pipeline.hset.call_count == 2
        pipeline.execute.assert_called_once()

    def test_embedding_content_fallback(self, indexer):
        knowledge = [
            {"table_name": "t1", "database": "db1", "description": "desc only"},
        ]
        indexer.embedding_service.embed.return_value = [[0.0] * 3]
        indexer.index_knowledge(knowledge)
        called_texts = indexer.embedding_service.embed.call_args[0][0]
        assert called_texts[0] == "desc only"

    def test_default_key_format(self, indexer):
        knowledge = [{"table_name": "t1", "database": "db1"}]
        indexer.index_knowledge(knowledge)
        call_args = indexer.redis.pipeline.return_value.hset.call_args
        assert call_args[0][0] == "test_idx:db1:t1"


class TestDropIndexAndInfo:
    """drop_index / index_exists / get_index_info"""

    def test_drop_index(self, indexer):
        indexer.drop_index()
        indexer.redis.ft.return_value.dropindex.assert_called_once_with(
            delete_documents=True
        )

    def test_index_exists_true(self, indexer):
        indexer.redis.ft.return_value.info.return_value = {}
        assert indexer.index_exists() is True

    def test_index_exists_false(self, indexer):
        indexer.redis.ft.return_value.info.side_effect = Exception("missing")
        assert indexer.index_exists() is False

    def test_get_index_info(self, indexer):
        indexer.redis.ft.return_value.info.return_value = {"index_name": "test_idx"}
        info = indexer.get_index_info()
        assert info["index_name"] == "test_idx"
