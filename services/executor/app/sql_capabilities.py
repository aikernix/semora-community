"""Bounded read-only probes, run on explicit source validation, never per question."""
import psycopg
from semora_contracts.database import SQL_BASELINE_ID, normalize_sql_probe
from . import mysql_adapter
from semora_contracts.sql_probes import PROBE_CATALOG_VERSION, probe_catalog, probe_matches

def probe_native(kind, dsn, keys=None):
    from . import adapter_registry
    protocol = adapter_registry.protocol(kind)
    if protocol not in {"mysql","postgres"}:
        raise ValueError("native capability probe requires an explicit adapter")
    dialect = "mysql" if protocol=="mysql" else "postgres"
    queries = probe_catalog(dialect)
    selected = set(queries) if keys is None else set(keys)
    if not selected <= queries.keys(): raise ValueError("unknown probe key")
    factory=(lambda:mysql_adapter.connect(dsn,3000)) if protocol=="mysql" else (lambda:psycopg.connect(dsn,connect_timeout=5))
    probes={}
    with factory() as connection:
        for feature, spec in queries.items():
            if feature not in selected: continue
            if spec.get("conversion_unavailable"):
                probes[feature] = {"status": "unverified", "reason": "probe_conversion_unavailable"}
                continue
            try:
                if protocol=="mysql":mysql_adapter.begin_readonly(connection,3000)
                with connection.cursor() as cursor:
                    if protocol=="postgres":
                        cursor.execute("SET TRANSACTION READ ONLY")
                        cursor.execute("SET LOCAL statement_timeout='3s'")
                    cursor.execute(spec["sql"])
                    row=cursor.fetchone()
                    if not probe_matches(row, spec) or cursor.fetchone() is not None:raise ValueError("unexpected capability probe result")
                probes[feature]={"status":"verified","method":"synthetic_readonly_probe", "signature": feature, "scope": "synthetic_signature"}
            except Exception as exc:
                # Failure means unavailable, not proof of a missing SQL feature.
                code=getattr(exc,"sqlstate",None)
                if code is None and exc.args and isinstance(exc.args[0],int):code=exc.args[0]
                probes[feature]=normalize_sql_probe({"status":"unavailable","code":code}, dialect)
            finally:connection.rollback()
    return {"sql_baseline":SQL_BASELINE_ID,**probes}
