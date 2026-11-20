from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from typing import Any
from uuid import uuid4

import httpx
import psycopg
from semora_contracts.database import PRODUCTS, native_protocol

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DataSourceRecord:
    id: str
    name: str
    type: str
    secret_ref: str
    status: str = "unverified"
    access: str = "read_only"
    objects: int = 0
    last_scan_at: datetime | None = None
    scan_result: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    capability_profile_id: str | None = None

    def public(self) -> dict[str, Any]:
        from semora_contracts.database import SQL_BASELINE_ID
        capabilities=dict(self.scan_result.get("capabilities",{}))
        if not native_protocol(self.type) and capabilities.get("sql_baseline")!=SQL_BASELINE_ID:
            capabilities["query"]="pending"
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "secret_ref": self.secret_ref,
            "status": self.status,
            "access": self.access,
            "objects": self.objects,
            "last_scan_at": self.last_scan_at,
            "created_at": self.created_at,
            "capability_profile_id": self.capability_profile_id,
            "connection": self.scan_result.get("connection", {}),
            "capabilities": capabilities,
            "scan_job": self.scan_result.get("scan_job"),
        }


class DataSourceRepository:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn if dsn is not None else os.getenv("METADATA_DATABASE_URL")
        self._items: dict[str, DataSourceRecord] = {}
        self._table_digests: dict[str, str] = {}
        from .capability_cache import CapabilityCache
        self.capability_cache = CapabilityCache(self._dsn)
        if self._dsn:
            self._load()
        if not self._dsn or os.getenv("SEMORA_DEMO_SEED", "false").lower() == "true":
            self._ensure_demo_source()

    def _connect(self):
        return psycopg.connect(self._dsn, autocommit=True)

    def _load(self) -> None:
        from .capability_storage import join_snapshot
        from .datasource_table_storage import load_tables, tables_digest
        tables_present: set[str] = set()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT d.id,d.name,d.type,d.secret_ref,d.status,d.access,d.objects,
                d.last_scan_at,d.scan_result,d.created_at,d.capability_profile_id,
                o.connection_metadata,o.capabilities,o.probes,o.observations,o.reuse_receipt
                FROM semora_meta.datasources d
                LEFT JOIN semora_meta.datasource_capability_observations o ON o.datasource_id=d.id
                WHERE d.status <> 'deleted'
                ORDER BY d.created_at""")
            for row in cursor.fetchall():
                stored_scan = dict(row[8] or {})
                if stored_scan.pop("_schema_tables_present", False):
                    tables_present.add(row[0])
                snapshot = join_snapshot(stored_scan, *(value or {} for value in row[11:16]))
                self._items[row[0]] = DataSourceRecord(
                    id=row[0], name=row[1], type=row[2], secret_ref=row[3], status=row[4],
                    access=row[5], objects=row[6], last_scan_at=row[7],
                    scan_result=snapshot, created_at=row[9], capability_profile_id=row[10],
                )
            stored_tables = load_tables(cursor)
            for identifier, item in self._items.items():
                # A completed empty scan is observably different from a source
                # that has never been scanned.
                if (
                    identifier in stored_tables
                    or identifier in tables_present
                    or item.last_scan_at is not None
                ):
                    item.scan_result["tables"] = stored_tables.get(identifier, [])
                    self._table_digests[identifier] = tables_digest(item.scan_result["tables"])
        # Rebuild the transient shared view from its canonical store after restart.
        # A cache failure must not discard the local observations just loaded.
        for item in list(self._items.values()):
            from .capability_cache import profile_descriptor, profile_key
            descriptor = profile_descriptor(item.type, item.scan_result.get("connection", {}),
                                              item.scan_result.get("capabilities", {}))
            expected = profile_key(descriptor) if descriptor else None
            if item.capability_profile_id != expected:
                self.save(item)  # Backfill legacy identities even without an old shared-cache reference.
            try:
                cached = self.capability_cache.resolve(item.type, item.scan_result.get("connection", {}),
                                                       item.scan_result.get("capabilities", {}))
                if cached:
                    receipt = item.scan_result.get("capability_cache") or {}
                    item.scan_result["capability_cache"] = {**receipt, **cached}
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("Capability cache reload unavailable: %s", type(exc).__name__)

    def _ensure_demo_source(self) -> None:
        if "retail-credit-pg" not in self._items:
            self.save(DataSourceRecord(
                id="retail-credit-pg",
                name="零售信贷演示库",
                type="postgresql",
                secret_ref="DATASOURCE_RETAIL_CREDIT_PG_URL",
            ))

    def save(self, item: DataSourceRecord) -> DataSourceRecord:
        from .capability_cache import profile_descriptor, profile_key
        from .capability_storage import split_snapshot, write_observations
        from .datasource_table_storage import split_tables, tables_digest, write_tables
        descriptor = profile_descriptor(item.type, item.scan_result.get("connection", {}),
                                         item.scan_result.get("capabilities", {}))
        item.capability_profile_id = profile_key(descriptor) if descriptor else None
        scan, state = split_snapshot(item.scan_result)
        scan, tables = split_tables(scan)
        snapshot_had_tables = tables is not None
        # Clearing a connection also clears its former schema rows.  Ordinary
        # metadata-only saves leave the table collection untouched.
        if (
            tables is None
            and item.last_scan_at is None
            and item.objects == 0
            and item.id in self._table_digests
        ):
            tables = []
        if snapshot_had_tables:
            scan["_schema_tables_present"] = True
        else:
            scan.pop("_schema_tables_present", None)
        table_digest = tables_digest(tables) if tables is not None else None
        if self._dsn:
            with self._connect() as connection, connection.transaction(), connection.cursor() as cursor:
                if descriptor:
                    cursor.execute("""INSERT INTO semora_meta.database_capability_profiles
                        (profile_key,descriptor,payload) VALUES(%s,%s::jsonb,'{}')
                        ON CONFLICT(profile_key) DO NOTHING""",
                        (item.capability_profile_id, json.dumps(descriptor)))
                cursor.execute(
                    """INSERT INTO semora_meta.datasources(
                        id,name,type,secret_ref,status,access,objects,last_scan_at,scan_result,created_at,capability_profile_id
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,type=excluded.type,secret_ref=excluded.secret_ref,
                        status=excluded.status,access=excluded.access,objects=excluded.objects,
                        last_scan_at=excluded.last_scan_at,scan_result=excluded.scan_result,
                        capability_profile_id=excluded.capability_profile_id
                    WHERE semora_meta.datasources.status <> 'deleted'""",
                    (item.id, item.name, item.type, item.secret_ref, item.status, item.access,
                     item.objects, item.last_scan_at, json.dumps(scan, ensure_ascii=False),
                     item.created_at, item.capability_profile_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError("数据源已删除")
                write_observations(cursor, item.id, state)
                if tables is not None and self._table_digests.get(item.id) != table_digest:
                    write_tables(cursor, item.id, tables, item.last_scan_at)
                    if item.scan_result.get('scan_manifest',{}).get('complete') is True:
                        from .relationship_inputs import build_inputs, write_inputs
                        write_inputs(cursor,build_inputs({item.id:{t['id']:t for t in tables}}),[item.id])
            if table_digest is not None:
                self._table_digests[item.id] = table_digest
        self._items[item.id] = item
        return item

    def create(self, name: str, datasource_type: str, secret_ref: str) -> DataSourceRecord:
        return self.save(DataSourceRecord(
            id=str(uuid4()), name=name, type=datasource_type, secret_ref=secret_ref,
        ))

    def evict(self, datasource_id: str) -> None:
        self._items.pop(datasource_id, None)
        self._table_digests.pop(datasource_id, None)

    def list(self) -> list[DataSourceRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)

    def get(self, datasource_id: str) -> DataSourceRecord:
        try:
            return self._items[datasource_id]
        except KeyError as exc:
            raise KeyError("data source not found") from exc

    def table_page(self, datasource_id: str, query: str = "", page: int = 1, page_size: int = 20):
        self.get(datasource_id)
        query = query.strip()
        if self._dsn:
            pattern = f"%{query}%"
            where = """datasource_id=%s AND (%s='' OR object_name ILIKE %s
                OR schema_name ILIKE %s OR description ILIKE %s)"""
            params = (datasource_id, query, pattern, pattern, pattern)
            with self._connect() as connection:
                total = connection.execute(
                    f"SELECT count(*) FROM semora_meta.datasource_tables WHERE {where}", params
                ).fetchone()[0]
                rows = connection.execute(
                    f"""SELECT table_id,catalog_name,schema_name,object_name,object_kind,description,
                    COALESCE(jsonb_array_length(columns),0),COALESCE(primary_keys,'[]'::jsonb)
                    FROM semora_meta.datasource_tables WHERE {where}
                    ORDER BY ordinal LIMIT %s OFFSET %s""",
                    (*params, page_size, (page - 1) * page_size),
                ).fetchall()
            items = [
                {
                    "id": row[0], "catalog": row[1], "schema": row[2], "name": row[3],
                    "kind": row[4], "description": row[5] or "", "column_count": row[6],
                    "primary_keys": row[7] or [],
                }
                for row in rows
            ]
        else:
            values = list(self.get(datasource_id).scan_result.get("tables") or [])
            folded = query.casefold()
            if folded:
                values = [
                    item for item in values
                    if folded in " ".join(str(item.get(key) or "") for key in (
                        "catalog", "schema", "name", "description"
                    )).casefold()
                ]
            total = len(values)
            window = values[(page - 1) * page_size:page * page_size]
            items = [
                {
                    "id": item.get("id"), "catalog": item.get("catalog"),
                    "schema": item.get("schema"), "name": item.get("name"),
                    "kind": item.get("kind"), "description": item.get("description") or "",
                    "column_count": len(item.get("columns") or []),
                    "primary_keys": item.get("primary_keys") or [],
                }
                for item in window
            ]
        return {"items": items, "page": page, "page_size": page_size, "total": total}

    def table_detail(self, datasource_id: str, table_id: str):
        item = self.get(datasource_id)
        if self._dsn:
            from .datasource_table_storage import decode_table
            with self._connect() as connection:
                row = connection.execute(
                    """SELECT table_id,ordinal,catalog_name,schema_name,object_name,table_name,
                    object_kind,description,columns,primary_keys,unique_constraints,foreign_keys,
                    indexes,extra,present_fields FROM semora_meta.datasource_tables
                    WHERE datasource_id=%s AND table_id=%s""",
                    (datasource_id, table_id),
                ).fetchone()
            if not row:
                raise KeyError(table_id)
            return decode_table(row)
        value = next(
            (table for table in item.scan_result.get("tables") or [] if table.get("id") == table_id),
            None,
        )
        if value is None:
            raise KeyError(table_id)
        return value

    @staticmethod
    def invalidate_changed_metadata(item, fresh):
        previous=item.scan_result.get("connection",{})
        if any(previous.get(k) is not None and fresh.get(k) is not None and previous[k]!=fresh[k]
               for k in ("product","version","driver_version","compatibility_mode","adapter_fingerprint","connection_fingerprint")):
            item.scan_result["connection"]={k:v for k,v in previous.items() if k!="probes"}
            item.scan_result["capabilities"]={}
            item.scan_result.pop("sql_feedback", None)
            item.scan_result.pop("sql_observations", None)
            item.scan_result.pop("capability_cache", None)
            item.scan_result["scan_stale"]=True

    def record_test(self, datasource_id: str, result: dict[str, Any]) -> DataSourceRecord:
        item = self.get(datasource_id)
        self.invalidate_changed_metadata(item,result)
        if isinstance(result.get("probes"), dict):
            from semora_contracts.database import normalize_sql_probe
            dialect = (result.get("capabilities") or {}).get("dialect") or result.get("dialect") or {"postgresql": "postgres", "sqlserver": "tsql"}.get(item.type, item.type)
            old_probes = item.scan_result.get("connection", {}).get("probes", {})
            old_capabilities = item.scan_result.get("capabilities", {})
            probes = dict(old_probes)
            for key, probe in result["probes"].items():
                fresh = normalize_sql_probe(probe, dialect)
                previous = normalize_sql_probe(old_probes.get(key) or old_capabilities.get(key), dialect)
                if fresh["status"] == "unverified" and previous["status"] in {"verified", "unsupported"}:
                    fresh = {**previous, "last_attempt": fresh}
                probes[key] = fresh
            result = {**result, "probes": probes, "capabilities": {**result.get("capabilities", {}), **probes}}
        item.status = "healthy" if result.get("read_only") else "unsafe"
        item.access = "read_only" if result.get("read_only") else "write_capable"
        if not native_protocol(item.type) and not result.get("read_only"):
            item.status="scan_only"
            item.access="scan_only"
        item.scan_result = {**item.scan_result, "connection": {
            **item.scan_result.get("connection", {}), **{k:v for k,v in result.items() if k not in {"capabilities", "datasource_id"}}}}
        capabilities=dict(item.scan_result.get("capabilities",{}))
        capabilities["connection"]="verified"
        if "capabilities" in result:
            capabilities.update({k:v for k,v in result["capabilities"].items() if v!="pending" or k not in capabilities})
        else:
            capabilities.setdefault("query","partial" if result.get("read_only") else "pending")
            dialect=PRODUCTS.get(item.type,{}).get("dialect")
            if dialect:capabilities["dialect"]=dialect
        if not result.get("read_only"):capabilities["query"]="pending"
        item.scan_result["capabilities"]=capabilities
        return self.save(item)

    def record_scan(self, datasource_id: str, result: dict[str, Any]) -> DataSourceRecord:
        from .scan_completion import completed_scan
        from copy import deepcopy
        result = completed_scan(result)
        item = deepcopy(self.get(datasource_id))
        self.invalidate_changed_metadata(item,result.get("connection",{}))
        sql_feedback = item.scan_result.get("sql_feedback", [])
        reusable = {k: item.scan_result[k] for k in ("capability_cache", "sql_observations") if k in item.scan_result}
        item.status = "healthy"
        item.access = "read_only"
        if result.get("connection",{}).get("read_only") is False:
            item.status="scan_only"
            item.access="scan_only"
        item.objects = len(result.get("tables", []))
        item.last_scan_at = utc_now()
        from .mdl_export import merge_snapshot
        if native_protocol(item.type) and item.scan_result.get("capabilities",{}).get("query")=="verified":
            result.setdefault("capabilities",{})["query"]="verified"
        result = merge_snapshot(item.scan_result, result, infer_relationships=False)
        result.update(reusable)
        if sql_feedback:
            result["sql_feedback"] = sql_feedback
        item.scan_result = {**result, "connection": {
            **(item.scan_result.get("connection") or {}), **(result.get("connection") or {})}}
        return self.save(item)


class ExecutorDataSourceClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("EXECUTOR_URL", "http://localhost:8002")).rstrip("/")
        self.token = token or os.getenv("INTERNAL_TOKEN", "semora-dev-internal")

    async def configuration(self, item: DataSourceRecord, connection=None, *, test=False):
        endpoint = "test-configuration" if test else "configuration"
        method = "PUT" if connection is not None and not test else "POST"
        payload = {"datasource_id": item.id, "type": item.type, "secret_ref": item.secret_ref}
        if connection is not None:
            payload["connection"] = connection
        async with httpx.AsyncClient(timeout=35) as client:
            try:
                response = await client.request(method, f"{self.base_url}/internal/v1/datasources/{endpoint}",
                    headers={"X-Semora-Internal-Token": self.token}, json=payload)
                if not response.is_success:
                    detail = response.json().get("detail", "Executor 配置请求失败")
                    raise RuntimeError(detail if isinstance(detail, str) else "连接参数无效，请检查各项输入")
                return response.json()
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Executor 配置请求失败：{type(exc).__name__}") from exc

    async def request(self, endpoint: str, item: DataSourceRecord, tables: list[str] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                response = await client.post(
                    f"{self.base_url}/internal/v1/datasources/{endpoint}",
                    headers={"X-Semora-Internal-Token": self.token},
                    json={"datasource_id": item.id, "type": item.type, "secret_ref": item.secret_ref, **({"tables": tables} if tables is not None else {})},
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Executor 数据源操作失败（HTTP {exc.response.status_code}）：{exc.response.text[:500]}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Executor 数据源请求失败：{type(exc).__name__}") from exc


class DataSourceService:
    def __init__(
        self,
        repository: DataSourceRepository | None = None,
        executor: ExecutorDataSourceClient | None = None,
    ) -> None:
        self.repository = repository or DataSourceRepository()
        self.executor = executor or ExecutorDataSourceClient()

    async def test(self, datasource_id: str) -> dict[str, Any]:
        item = self.repository.get(datasource_id)
        try:
            result = await self.executor.request("test", item)
        except RuntimeError:
            item.status="failed"
            item.scan_result.setdefault("capabilities",{}).update(connection="failed",query="pending")
            self.repository.save(item)
            raise
        self.repository.record_test(datasource_id, result)
        return result

    async def scan(self, datasource_id: str, tables: list[str] | None = None) -> dict[str, Any]:
        item = self.repository.get(datasource_id)
        result = await self.executor.request("scan", item, tables=tables) if tables is not None else await self.executor.request("scan", item)
        self.repository.record_scan(datasource_id, result)
        return result


datasource_service = DataSourceService()
