"""greptimedb_source.py 单元测试（P0-GAP-02）"""

import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path

from xiyan_mcp_server.utils.greptimedb_source import GreptimeDBSource
from xiyan_mcp_server.utils.db_mschema import MSchema


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.dialect.name = "greptimedb"
    engine.url.database = "testdb"
    return engine


class TestGreptimeDBSourceInit:
    """初始化逻辑"""

    def test_basic_init(self, mock_engine):
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine, db_name="mydb", system_prefix="sys")
        assert src.db_name == "mydb"
        assert src._system_prefix == "sys"
        assert isinstance(src._loading_locks, dict)

    def test_db_name_from_url_when_empty(self, mock_engine):
        mock_engine.url.database = None
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine)
        assert src.db_name == "public"

    def test_cache_path_with_system_prefix(self, mock_engine):
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine, system_prefix="CockroachDB")
        path = src._get_schema_cache_path()
        assert path is not None
        assert "cockroachdb" in path.lower()
        assert path.endswith("schema_cache.json")

    def test_cache_path_none_without_prefix(self, mock_engine):
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine, system_prefix="")
        assert src._get_schema_cache_path() is None


class TestGetTableLoadingLock:
    """LRU 锁缓存"""

    def test_new_lock_created(self, mock_engine):
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine)
        lock = src._get_table_loading_lock("t1")
        assert lock is not None
        assert "t1" in src._loading_locks

    def test_existing_lock_returned(self, mock_engine):
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine)
        lock1 = src._get_table_loading_lock("t1")
        lock2 = src._get_table_loading_lock("t1")
        assert lock1 is lock2

    def test_locks_kept_forever(self, mock_engine):
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine)
        src._get_table_loading_lock("t1")
        src._get_table_loading_lock("t2")
        src._get_table_loading_lock("t3")
        src._get_table_loading_lock("t4")
        assert len(src._loading_locks) == 4
        assert "t1" in src._loading_locks
        assert "t4" in src._loading_locks


class TestLoadTableColumns:
    """延迟加载列信息"""

    def test_skip_when_fields_exist(self, mock_engine):
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine)
        ms = MSchema(db_id="db")
        ms.add_table("s.t")
        ms.add_field("s.t", "x", field_type="INT")
        src._mschema = ms
        src._engine = mock_engine
        src._load_table_columns("s", "t")
        assert "x" in ms.tables["s.t"]["fields"]

    def test_loads_columns_when_empty(self, mock_engine):
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine)
        ms = MSchema(db_id="db")
        ms.add_table("s.t")
        src._mschema = ms
        src._engine = mock_engine
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda self: iter(
            [
                ("id", "INT64", "YES", None),
                ("name", "STRING", "NO", None),
            ]
        )
        mock_conn.execute.return_value = mock_result
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        with patch.object(src, "_fetch_distinct_values", return_value=[]):
            src._load_table_columns("s", "t")
        cols = ms.tables["s.t"]["fields"]
        assert "id" in cols
        assert cols["id"]["type"] == "BIGINT"
        assert cols["name"]["type"] == "VARCHAR"


class TestFetchDistinctValuesSQLInjection:
    """SQL 注入防护"""

    def _make_source(self, mock_engine):
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine)
        return src

    def test_safe_table_name(self, mock_engine):
        src = self._make_source(mock_engine)
        src._fetch_distinct_values("schema", "table_a", "col1", 5)

    def test_sql_injection_single_quote_blocked(self, mock_engine):
        src = self._make_source(mock_engine)
        with pytest.raises(ValueError, match="表名格式无效"):
            src._fetch_distinct_values("schema", "table'; DROP TABLE t; --", "col1", 5)

    def test_sql_injection_semicolon_blocked(self, mock_engine):
        src = self._make_source(mock_engine)
        with pytest.raises(ValueError, match="表名格式无效"):
            src._fetch_distinct_values("schema", "table; DELETE FROM t", "col1", 5)

    def test_sql_injection_comment_blocked(self, mock_engine):
        src = self._make_source(mock_engine)
        with pytest.raises(ValueError, match="表名格式无效"):
            src._fetch_distinct_values("schema", "table--comment", "col1", 5)

    def test_path_traversal_blocked(self, mock_engine):
        src = self._make_source(mock_engine)
        with pytest.raises(ValueError, match="表名格式无效"):
            src._fetch_distinct_values("schema", "../etc/passwd", "col1", 5)


class TestMapType:
    """_map_type 类型映射"""

    def test_common_mappings(self, mock_engine):
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine)
        assert src._map_type("INT64") == "BIGINT"
        assert src._map_type("STRING") == "VARCHAR"
        assert src._map_type("FLOAT64") == "DOUBLE PRECISION"

    def test_unknown_passthrough(self, mock_engine):
        with patch.object(GreptimeDBSource, "init_mschema"):
            src = GreptimeDBSource(mock_engine)
        assert src._map_type("UNKNOWN_TYPE") == "UNKNOWN_TYPE"
