"""schema_retriever.py 单元测试（P0-GAP-03）"""

import json
import pytest
from unittest.mock import MagicMock, patch

from xiyan_mcp_server.utils.schema_retriever import SchemaRetriever
from xiyan_mcp_server.utils.db_mschema import MSchema


@pytest.fixture
def mock_redis():
    return MagicMock()


@pytest.fixture
def mock_embed():
    e = MagicMock()
    e.embed_single.return_value = [0.1, 0.2, 0.3]
    return e


@pytest.fixture
def retriever(mock_redis, mock_embed):
    ms = MSchema(db_id="db1")
    ms.add_table("db1.t1")
    ms.add_table("db1.t2")
    cfg = {
        "index_name": "test_idx",
        "top_k": 5,
        "score_threshold": 0.6,
    }
    return SchemaRetriever(mock_redis, mock_embed, ms, cfg)


class TestGetMatchedDatabaseTags:
    """_get_matched_database_tags"""

    def test_returns_matched_tags(self, retriever):
        retriever.redis.execute_command.return_value = [
            b"cockroachdb_metrics",
            b"cockroachdb_logs",
            b"other_db",
        ]
        result = retriever._get_matched_database_tags("cockroach")
        assert "cockroachdb_metrics" in result
        assert "cockroachdb_logs" in result
        assert "other_db" not in result

    def test_case_insensitive(self, retriever):
        retriever.redis.execute_command.return_value = [b"CockroachDB"]
        result = retriever._get_matched_database_tags("cockroach")
        assert result == ["CockroachDB"]

    def test_empty_tags_returns_empty(self, retriever):
        retriever.redis.execute_command.return_value = []
        assert retriever._get_matched_database_tags("x") == []

    def test_redis_error_returns_empty(self, retriever):
        retriever.redis.execute_command.side_effect = Exception("redis down")
        assert retriever._get_matched_database_tags("x") == []


class TestBuildSubSchema:
    """build_sub_schema"""

    def test_empty_table_names_returns_full_schema(self, retriever):
        result = retriever.build_sub_schema([])
        assert "【DB_ID】 db1" in result

    def test_skip_lazy_load_judge_path(self, retriever):
        result = retriever.build_sub_schema(["db1.t1"], skip_lazy_load=True)
        assert "1." in result or "t1" in result

    def test_case_insensitive_table_match(self, retriever):
        result = retriever.build_sub_schema(["DB1.T1"], skip_lazy_load=True)
        assert "t1" in result.lower()

    def test_lazy_load_called_when_fields_empty(self, retriever):
        ms = MSchema(db_id="db1")
        ms.add_table("db1.new_t")
        retriever.mschema = ms
        retriever.db_source = MagicMock()
        retriever.db_source._load_table_columns = MagicMock()
        retriever.build_sub_schema(["db1.new_t"])
        retriever.db_source._load_table_columns.assert_called_once_with("db1", "new_t")


class TestRetrieveTableNames:
    """retrieve_table_names"""

    def test_returns_lowercase_database_prefix(self, retriever):
        doc = MagicMock()
        doc.table_name = "sys_cpu"
        doc.database = "COCKROACHDB_METRICS"
        doc.score = 0.9
        doc.__getitem__ = lambda self, key: {
            "database": "COCKROACHDB_METRICS",
            "table_name": "sys_cpu",
        }[key]
        retriever.retrieve = MagicMock(return_value=[doc])
        result = retriever.retrieve_table_names("query", top_k=1)
        assert result == ["cockroachdb_metrics.sys_cpu"]


class TestRetrieveAndBuild:
    """retrieve_and_build"""

    def test_builds_sub_schema(self, retriever):
        retriever.retrieve_table_names = MagicMock(return_value=["db1.t1", "db1.t2"])
        retriever.build_sub_schema = MagicMock(return_value="sub schema")
        import asyncio

        result = asyncio.run(retriever.retrieve_and_build("query"))
        names, schema, meta = result
        assert names == ["db1.t1", "db1.t2"]
        assert schema == "sub schema"
        assert "stage1_tables" in meta
