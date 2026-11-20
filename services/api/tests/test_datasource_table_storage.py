from copy import deepcopy

from app.datasource_table_storage import decode_table, encode_table, split_tables, tables_digest
from app.mdl_export import build_mdl


def sample_snapshot():
    return {
        "snapshot_version": 2,
        "permission_gaps": [],
        "connection": {"database": "demo"},
        "tables": [
            {
                "id": "demo.public.customer",
                "catalog": "demo",
                "schema": "public",
                "name": "customer",
                "kind": "TABLE",
                "description": "客户",
                "columns": [
                    {"name": "id", "type": "integer", "raw_type": "int4", "nullable": False},
                    {
                        "name": "name",
                        "type": "varchar",
                        "raw_type": "varchar(80)",
                        "nullable": True,
                    },
                ],
                "primary_keys": [{"name": "customer_pkey", "columns": ["id"], "definition": ""}],
                "unique_constraints": [],
                "foreign_keys": [],
                "indexes": [{"name": "customer_name_idx", "columns": ["name"]}],
                "user_description": "客户主数据",
            }
        ],
        "foreign_keys": [],
        "relationship_candidates": [],
        "relationship_decisions": {},
        "manual_relationships": [],
    }


def test_table_storage_round_trip_preserves_snapshot_exactly():
    before = sample_snapshot()
    metadata, tables = split_tables(before)
    assert tables is not None and "tables" not in metadata
    encoded = encode_table(tables[0], 0, None)
    restored = {**metadata, "tables": [decode_table(encoded[:15])]}
    assert restored == before
    assert tables_digest(restored["tables"]) == tables_digest(before["tables"])


def test_table_storage_preserves_absent_and_empty_collections():
    metadata, tables = split_tables({"snapshot_version": 2})
    assert metadata == {"snapshot_version": 2} and tables is None
    metadata, tables = split_tables({"snapshot_version": 2, "tables": []})
    assert metadata == {"snapshot_version": 2} and tables == []


def test_wren_input_and_output_are_unchanged_by_storage_round_trip():
    before = sample_snapshot()
    metadata, tables = split_tables(deepcopy(before))
    restored_tables = [
        decode_table(encode_table(table, i, None)[:15])
        for i, table in enumerate(tables or [])
    ]
    restored = {**metadata, "tables": restored_tables}
    assert restored == before
    assert build_mdl(restored, "postgresql") == build_mdl(before, "postgresql")
