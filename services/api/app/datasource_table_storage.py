"""Separate table persistence with an unchanged runtime snapshot contract."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

TABLE_FIELDS = (
    "id", "catalog", "schema", "name", "table_name", "kind", "description",
    "columns", "primary_keys", "unique_constraints", "foreign_keys", "indexes",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def tables_digest(tables: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical(tables).encode()).hexdigest()


def split_tables(snapshot: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    """Separate tables without changing absent-versus-explicitly-empty semantics."""
    metadata = dict(snapshot)
    tables = metadata.pop("tables", None) if "tables" in metadata else None
    return metadata, tables


def table_identity(table: dict[str, Any], ordinal: int) -> str:
    identity = table.get("id") or ".".join(
        str(table.get(key) or "") for key in ("catalog", "schema", "name") if table.get(key)
    )
    return identity or f"legacy-table-{ordinal}"


def encode_table(table: dict[str, Any], ordinal: int, scanned_at: datetime | None):
    present = [key for key in TABLE_FIELDS if key in table]
    extra = {key: value for key, value in table.items() if key not in TABLE_FIELDS}
    content_hash = hashlib.sha256(_canonical(table).encode()).hexdigest()
    return (
        table_identity(table, ordinal), ordinal, table.get("catalog"), table.get("schema"),
        table.get("name"), table.get("table_name"), table.get("kind"), table.get("description"),
        json.dumps(table.get("columns"), ensure_ascii=False) if "columns" in table else None,
        (
            json.dumps(table.get("primary_keys"), ensure_ascii=False)
            if "primary_keys" in table else None
        ),
        (
            json.dumps(table.get("unique_constraints"), ensure_ascii=False)
            if "unique_constraints" in table else None
        ),
        (
            json.dumps(table.get("foreign_keys"), ensure_ascii=False)
            if "foreign_keys" in table else None
        ),
        json.dumps(table.get("indexes"), ensure_ascii=False) if "indexes" in table else None,
        json.dumps(extra, ensure_ascii=False), json.dumps(present),
        scanned_at or datetime.now(UTC), content_hash,
    )


def decode_table(row) -> dict[str, Any]:
    def parsed(value):
        return json.loads(value) if isinstance(value, str) else value

    present = set(parsed(row[14]) or [])
    table = dict(parsed(row[13]) or {})
    stored = (row[0], *row[2:8], *(parsed(value) for value in row[8:13]))
    values = dict(zip(TABLE_FIELDS, stored, strict=True))
    for key in TABLE_FIELDS:
        if key in present:
            table[key] = values[key]
    return table


def load_tables(cursor) -> dict[str, list[dict[str, Any]]]:
    cursor.execute(
        """SELECT datasource_id,table_id,ordinal,catalog_name,schema_name,object_name,
            table_name,object_kind,description,columns,primary_keys,unique_constraints,
            foreign_keys,indexes,extra,present_fields
            FROM semora_meta.datasource_tables ORDER BY datasource_id,ordinal"""
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in cursor.fetchall():
        grouped.setdefault(row[0], []).append(decode_table((row[1], row[2], *row[3:])))
    return grouped


def write_tables(
    cursor,
    datasource_id: str,
    tables: list[dict[str, Any]],
    scanned_at: datetime | None,
) -> None:
    rows = [encode_table(table, ordinal, scanned_at) for ordinal, table in enumerate(tables)]
    if not rows:
        cursor.execute(
            "DELETE FROM semora_meta.datasource_tables WHERE datasource_id=%s",
            (datasource_id,),
        )
        return
    cursor.executemany(
        """INSERT INTO semora_meta.datasource_tables(
            datasource_id,table_id,ordinal,catalog_name,schema_name,object_name,
            table_name,object_kind,description,columns,primary_keys,unique_constraints,
            foreign_keys,indexes,extra,present_fields,scanned_at,content_hash
        ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,
            %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)
        ON CONFLICT(datasource_id,table_id) DO UPDATE SET
            ordinal=excluded.ordinal,catalog_name=excluded.catalog_name,schema_name=excluded.schema_name,
            object_name=excluded.object_name,table_name=excluded.table_name,object_kind=excluded.object_kind,
            description=excluded.description,columns=excluded.columns,primary_keys=excluded.primary_keys,
            unique_constraints=excluded.unique_constraints,foreign_keys=excluded.foreign_keys,
            indexes=excluded.indexes,extra=excluded.extra,present_fields=excluded.present_fields,
            scanned_at=excluded.scanned_at,content_hash=excluded.content_hash
        WHERE semora_meta.datasource_tables.content_hash IS DISTINCT FROM excluded.content_hash
           OR semora_meta.datasource_tables.ordinal IS DISTINCT FROM excluded.ordinal""",
        [(datasource_id, *row) for row in rows],
    )
    cursor.execute(
        """DELETE FROM semora_meta.datasource_tables
            WHERE datasource_id=%s AND NOT (table_id = ANY(%s))""",
        (datasource_id, [row[0] for row in rows]),
    )
