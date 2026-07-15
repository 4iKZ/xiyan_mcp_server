"""greptimedb_dialect.py 单元测试（P0-GAP-01）"""

import pytest
from unittest.mock import MagicMock, PropertyMock

from xiyan_mcp_server.utils.greptimedb_dialect import (
    GreptimeDBInspector,
    GreptimeDBDialect,
)


class TestMapGreptimedbType:
    """GreptimeDB 类型映射"""

    def _inspector(self):
        insp = GreptimeDBInspector.__new__(GreptimeDBInspector)
        insp.bind = MagicMock()
        return insp

    def test_string_with_length(self):
        assert self._inspector()._map_greptimedb_type("STRING", 255) == "VARCHAR(255)"

    def test_string_without_length(self):
        assert self._inspector()._map_greptimedb_type("STRING") == "VARCHAR"

    def test_int64_maps_to_bigint(self):
        assert self._inspector()._map_greptimedb_type("INT64") == "BIGINT"

    def test_int32_maps_to_integer(self):
        assert self._inspector()._map_greptimedb_type("INT32") == "INTEGER"

    def test_int8_maps_to_smallint(self):
        assert self._inspector()._map_greptimedb_type("INT8") == "SMALLINT"

    def test_uint_types(self):
        i = self._inspector()
        assert i._map_greptimedb_type("UINT8") == "SMALLINT"
        assert i._map_greptimedb_type("UINT16") == "INTEGER"
        assert i._map_greptimedb_type("UINT32") == "BIGINT"
        assert i._map_greptimedb_type("UINT64") == "BIGINT"

    def test_float_types(self):
        i = self._inspector()
        assert i._map_greptimedb_type("FLOAT32") == "REAL"
        assert i._map_greptimedb_type("FLOAT64") == "DOUBLE PRECISION"

    def test_boolean_type(self):
        assert self._inspector()._map_greptimedb_type("BOOLEAN") == "BOOLEAN"

    def test_binary_type(self):
        assert self._inspector()._map_greptimedb_type("BINARY") == "BYTEA"

    def test_date_types(self):
        i = self._inspector()
        assert i._map_greptimedb_type("DATE") == "DATE"
        assert i._map_greptimedb_type("DATETIME") == "TIMESTAMP"
        assert i._map_greptimedb_type("TIMESTAMP") == "TIMESTAMP"
        assert i._map_greptimedb_type("TIMESTAMPTZ") == "TIMESTAMP WITH TIME ZONE"

    def test_unknown_type_passthrough(self):
        assert self._inspector()._map_greptimedb_type("UNKNOWN_TYPE") == "UNKNOWN_TYPE"

    def test_case_insensitive(self):
        i = self._inspector()
        assert i._map_greptimedb_type("string") == "VARCHAR"
        assert i._map_greptimedb_type("int64") == "BIGINT"


class TestGreptimeDBInspectorGetDefaultSchema:
    """_get_default_schema"""

    def _make_inspector(self, bind):
        insp = GreptimeDBInspector.__new__(GreptimeDBInspector)
        insp.bind = bind
        return insp

    def test_engine_bind_returns_database(self):
        class FakeEngine:
            url = type("URL", (), {"database": "mydb"})

        insp = self._make_inspector(FakeEngine())
        assert insp._get_default_schema() == "mydb"

    def test_connection_bind_returns_database(self):
        class FakeConn:
            url = type("URL", (), {"database": "mydb"})

        insp = self._make_inspector(FakeConn())
        assert insp._get_default_schema() == "mydb"

    def test_no_database_returns_public(self):
        class FakeEngine:
            url = type("URL", (), {"database": None})

        insp = self._make_inspector(FakeEngine())
        assert insp._get_default_schema() == "public"

    def test_exception_returns_public(self):
        class FakeEngine:
            @property
            def url(self):
                raise Exception("fail")

        insp = self._make_inspector(FakeEngine())
        assert insp._get_default_schema() == "public"


class TestGreptimeDBInspectorGetTableNames:
    """get_table_names"""

    def test_returns_table_list(self):
        inspector = GreptimeDBInspector.__new__(GreptimeDBInspector)
        inspector.bind = MagicMock()
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda self: iter([("t1",), ("t2",)])
        mock_conn.execute.return_value = mock_result
        inspector.bind.connect.return_value.__enter__ = MagicMock(
            return_value=mock_conn
        )
        inspector.bind.connect.return_value.__exit__ = MagicMock(return_value=False)

        tables = inspector.get_table_names(schema="myschema")
        assert tables == ["t1", "t2"]


class TestGreptimeDBInspectorGetColumns:
    """get_columns"""

    def test_returns_column_info(self):
        inspector = GreptimeDBInspector.__new__(GreptimeDBInspector)
        inspector.bind = MagicMock()
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda self: iter(
            [
                ("id", "INT64", "YES", None, None),
                ("name", "STRING", "NO", None, 100),
            ]
        )
        mock_conn.execute.return_value = mock_result
        inspector.bind.connect.return_value.__enter__ = MagicMock(
            return_value=mock_conn
        )
        inspector.bind.connect.return_value.__exit__ = MagicMock(return_value=False)

        cols = inspector.get_columns("t1", schema="myschema")
        assert len(cols) == 2
        assert cols[0]["name"] == "id"
        assert cols[0]["type"] == "BIGINT"
        assert cols[0]["nullable"] is True
        assert cols[1]["type"] == "VARCHAR(100)"
        assert cols[1]["nullable"] is False


class TestGreptimeDBInspectorHasTable:
    """has_table"""

    def test_returns_true_when_exists(self):
        inspector = GreptimeDBInspector.__new__(GreptimeDBInspector)
        inspector.bind = MagicMock()
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = (1,)
        mock_conn.execute.return_value = mock_result
        inspector.bind.connect.return_value.__enter__ = MagicMock(
            return_value=mock_conn
        )
        inspector.bind.connect.return_value.__exit__ = MagicMock(return_value=False)

        assert inspector.has_table("t1", schema="myschema") is True

    def test_returns_false_when_missing(self):
        inspector = GreptimeDBInspector.__new__(GreptimeDBInspector)
        inspector.bind = MagicMock()
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None
        mock_conn.execute.return_value = mock_result
        inspector.bind.connect.return_value.__enter__ = MagicMock(
            return_value=mock_conn
        )
        inspector.bind.connect.return_value.__exit__ = MagicMock(return_value=False)

        assert inspector.has_table("missing_t", schema="myschema") is False


class TestGreptimeDBInspectorEmptyReturns:
    """get_pk_constraint / get_foreign_keys / get_unique_constraints / get_indexes / get_table_comment"""

    def test_get_pk_constraint_returns_empty(self):
        inspector = GreptimeDBInspector.__new__(GreptimeDBInspector)
        assert inspector.get_pk_constraint("t1") == {
            "constrained_columns": [],
            "name": None,
        }

    def test_get_foreign_keys_returns_empty(self):
        inspector = GreptimeDBInspector.__new__(GreptimeDBInspector)
        assert inspector.get_foreign_keys("t1") == []

    def test_get_unique_constraints_returns_empty(self):
        inspector = GreptimeDBInspector.__new__(GreptimeDBInspector)
        assert inspector.get_unique_constraints("t1") == []

    def test_get_indexes_returns_empty(self):
        inspector = GreptimeDBInspector.__new__(GreptimeDBInspector)
        assert inspector.get_indexes("t1") == []

    def test_get_table_comment_returns_none(self):
        inspector = GreptimeDBInspector.__new__(GreptimeDBInspector)
        assert inspector.get_table_comment("t1") == {"text": None}


class TestGreptimeDBDialect:
    """GreptimeDBDialect 属性"""

    def test_name(self):
        assert GreptimeDBDialect.name == "greptimedb"

    def test_initialize_sets_attributes(self):
        d = GreptimeDBDialect()
        conn = MagicMock()
        d.initialize(conn)
        assert d.server_version_info == (14, 0)
        assert d.default_schema_name == "public"
        assert d._has_native_hstore is False

    def test_on_connect_returns_empty_list(self):
        d = GreptimeDBDialect()
        assert d.on_connect() == []
