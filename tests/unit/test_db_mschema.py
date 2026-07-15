"""db_mschema.py 单元测试（P0-GAP-05）"""

import json
import pytest

from xiyan_mcp_server.utils.db_mschema import MSchema


class TestMSchemaAddField:
    """add_table / add_field"""

    def test_add_table_creates_entry(self):
        ms = MSchema(db_id="db1")
        ms.add_table("users", fields={}, comment="user table")
        assert "users" in ms.tables
        assert ms.tables["users"]["comment"] == "user table"

    def test_add_field_stores_type_and_meta(self):
        ms = MSchema(db_id="db1")
        ms.add_table("users", fields={})
        ms.add_field("users", "id", field_type="INT", primary_key=True, nullable=False)
        f = ms.tables["users"]["fields"]["id"]
        assert f["type"] == "INT"
        assert f["primary_key"] is True
        assert f["nullable"] is False
        assert f["default"] is None

    def test_add_field_default_stringified(self):
        ms = MSchema(db_id="db1")
        ms.add_table("t", fields={})
        ms.add_field("t", "val", default=123)
        assert ms.tables["t"]["fields"]["val"]["default"] == "123"

    def test_add_field_examples_copied(self):
        ms = MSchema(db_id="db1")
        ms.add_table("t", fields={})
        ex = ["a", "b"]
        ms.add_field("t", "c", examples=ex)
        assert ms.tables["t"]["fields"]["c"]["examples"] == ["a", "b"]
        ex.append("c")
        assert ms.tables["t"]["fields"]["c"]["examples"] == ["a", "b"]


class TestMSchemaQuery:
    """has_table / has_column / get_field_type / get_field_info"""

    def test_has_table_true_false(self):
        ms = MSchema()
        ms.add_table("t1")
        assert ms.has_table("t1") is True
        assert ms.has_table("t2") is False

    def test_has_column(self):
        ms = MSchema()
        ms.add_table("t1")
        ms.add_field("t1", "c1")
        assert ms.has_column("t1", "c1") is True
        assert ms.has_column("t1", "c2") is False
        assert ms.has_column("t2", "c1") is False

    def test_get_field_type_simple_mode(self):
        ms = MSchema()
        assert ms.get_field_type("VARCHAR(255)", simple_mode=True) == "VARCHAR"
        assert ms.get_field_type("VARCHAR(255)", simple_mode=False) == "VARCHAR(255)"
        assert ms.get_field_type("INT", simple_mode=True) == "INT"

    def test_get_field_info_existing(self):
        ms = MSchema()
        ms.add_table("t1")
        ms.add_field("t1", "c1", field_type="INT")
        info = ms.get_field_info("t1", "c1")
        assert info["type"] == "INT"

    def test_get_field_info_missing_table(self):
        ms = MSchema()
        assert ms.get_field_info("missing", "c") == {}

    def test_get_field_info_missing_column(self):
        ms = MSchema()
        ms.add_table("t1")
        assert ms.get_field_info("t1", "missing") == {}


class TestSingleTableMschema:
    """single_table_mschema 格式化"""

    def test_no_schema_prefix(self):
        ms = MSchema(db_id="db1")
        ms.add_table("users", comment="User table")
        ms.add_field("users", "id", field_type="INT", primary_key=True)
        out = ms.single_table_mschema(
            "users", example_num=0, show_type_detail=False, shuffle=False
        )
        assert "# Table: users, User table" in out
        assert "(id:INT, Primary Key)" in out

    def test_with_schema_prefix(self):
        ms = MSchema(db_id="db1", schema="myschema")
        ms.add_table("t", comment="")
        ms.add_field("t", "x", field_type="VARCHAR(10)")
        out = ms.single_table_mschema("t", example_num=0, shuffle=False)
        assert "# Table: myschema.t" in out

    def test_examples_truncated(self):
        ms = MSchema(db_id="db1")
        ms.add_table("t")
        ms.add_field("t", "x", field_type="VARCHAR", examples=["a" * 60])
        out = ms.single_table_mschema("t", example_num=3, shuffle=False)
        assert "Examples:" not in out

    def test_no_comment_no_comment_suffix(self):
        ms = MSchema(db_id="db1")
        ms.add_table("t", comment="")
        ms.add_field("t", "x", field_type="INT")
        out = ms.single_table_mschema("t", example_num=0, shuffle=False)
        assert "# Table: t" in out


class TestToMschema:
    """to_mschema 表/列过滤 + 截断"""

    def test_all_tables(self):
        ms = MSchema(db_id="db1")
        ms.add_table("t1")
        ms.add_table("t2")
        ms.add_field("t1", "x")
        ms.add_field("t2", "y")
        out = ms.to_mschema(example_num=0, shuffle=False)
        assert "【DB_ID】 db1" in out
        assert "# Table: t1" in out
        assert "# Table: t2" in out

    def test_selected_tables_filter(self):
        ms = MSchema(db_id="db1")
        ms.add_table("t1")
        ms.add_table("t2")
        ms.add_field("t1", "x")
        ms.add_field("t2", "y")
        out = ms.to_mschema(selected_tables=["t1"], example_num=0, shuffle=False)
        assert "# Table: t1" in out
        assert "# Table: t2" not in out

    def test_selected_columns_filter(self):
        ms = MSchema(db_id="db1")
        ms.add_table("t1")
        ms.add_field("t1", "x")
        ms.add_field("t1", "y")
        out = ms.to_mschema(selected_columns=["t1.x"], example_num=0, shuffle=False)
        assert "x" in out

    def test_max_tables_truncation(self):
        ms = MSchema(db_id="db1")
        for i in range(5):
            ms.add_table(f"t{i}")
            ms.add_field(f"t{i}", "x")
        out = ms.to_mschema(max_tables=2, example_num=0, shuffle=False)
        table_count = sum(1 for line in out.splitlines() if line.startswith("# Table:"))
        assert table_count == 2


class TestMSchemaDumpSaveLoad:
    """序列化/反序列化"""

    def test_dump_contains_keys(self):
        ms = MSchema(db_id="db1", schema="s1")
        ms.add_table("t1")
        ms.add_field("t1", "x")
        d = ms.dump()
        assert d["db_id"] == "db1"
        assert d["schema"] == "s1"
        assert "t1" in d["tables"]

    def test_save_and_load_roundtrip(self, tmp_path):
        ms = MSchema(db_id="db1", schema="s1")
        ms.add_table("t1")
        ms.add_field("t1", "x", field_type="INT")
        f = tmp_path / "schema.json"
        ms.save(str(f))
        assert f.exists()

        ms2 = MSchema()
        ms2.load(str(f))
        assert ms2.db_id == "db1"
        assert ms2.schema == "s1"
        assert ms2.has_table("t1")
        assert ms2.has_column("t1", "x")
        assert ms2.get_field_info("t1", "x")["type"] == "INT"
