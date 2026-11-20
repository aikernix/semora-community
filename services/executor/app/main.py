from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import threading
import time
from typing import Any, Literal

import psycopg
import sqlglot
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from semora_contracts.query_limits import (
    EXPLAIN_TIMEOUT_DEFAULT_SECONDS, MIB, RESULT_DEFAULT_MIB, RESULT_DEFAULT_ROWS,
)
from sqlglot import exp
from semora_telemetry import configure_telemetry
from . import mysql_adapter, jdbc_adapter, adapter_registry, drivers, scan_control
from .errors import database_error
from .connection_settings import ConnectionSettings, connection_store


from .timeouts import scan_timeout_ms, query_timeout_ms, relationship_profile_timeout_ms

app = FastAPI(title="Semora Read-only Executor", version="0.1.0")
@app.exception_handler(RequestValidationError)
async def private_validation_error(request, exc):
    # Pydantic root-validator errors can otherwise echo the full password-bearing input.
    return JSONResponse(status_code=422, content={"detail":[{k:e[k] for k in ("loc","msg","type") if k in e} for e in exc.errors()]})

configure_telemetry(app, "semora-executor")
FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Merge)
ACTIVE_QUERIES: dict[str, psycopg.Connection] = {}
ACTIVE_QUERIES_LOCK = threading.Lock()


class DataSourceInput(BaseModel):
    datasource_id: str = Field(min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=32)
    secret_ref: str = Field(pattern=r"^DATASOURCE_[A-Z0-9_]+_URL$", max_length=200)


class DataSourceScanInput(DataSourceInput):
    tables: list[str] | None = Field(default=None, min_length=1, max_length=1000)
    execution_id: str | None = Field(default=None, max_length=64)


class DataSourceValidationInput(DataSourceInput):
    probe_keys: list[str] | None = Field(default=None, max_length=128)


class QueryInput(BaseModel):
    sql: str = Field(min_length=1, max_length=100_000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    sensitive_columns: list[str] = Field(default_factory=list, max_length=1000)
    timeout_ms: int | None = Field(default=None, ge=100)
    max_rows: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=1_024)
    execution_id: str | None = Field(default=None, min_length=1, max_length=64)
    datasource: DataSourceInput


class EstimateInput(BaseModel):
    sql: str = Field(min_length=1, max_length=100_000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=EXPLAIN_TIMEOUT_DEFAULT_SECONDS * 1000, ge=100)
    datasource: DataSourceInput


def require_internal_token(token: str | None) -> None:
    expected = os.getenv("INTERNAL_TOKEN", "semora-dev-internal")
    if os.getenv("REQUIRE_INTERNAL_TOKEN", "false").lower() == "true" and token != expected:
        raise HTTPException(401, "invalid internal token")


def resolve_datasource(body: DataSourceInput) -> str:
    if adapter_registry.is_jdbc(body.type):
        raise HTTPException(422, f"当前版本不支持数据源类型：{body.type}")
    value = connection_store.resolve(body.datasource_id, body.secret_ref)
    if not value:
        raise HTTPException(422, f"Executor 未配置密钥引用：{body.secret_ref}")
    return value


class ConfigureDataSource(DataSourceInput):
    connection: ConnectionSettings


@app.post("/internal/v1/datasources/configuration")
def read_connection(body: DataSourceInput, x_semora_internal_token: str | None = Header(default=None)):
    require_internal_token(x_semora_internal_token)
    return connection_store.public(connection_store.get(body.datasource_id, body.secret_ref, body.type))


@app.put("/internal/v1/datasources/configuration")
def save_connection(body: ConfigureDataSource, x_semora_internal_token: str | None = Header(default=None)):
    require_internal_token(x_semora_internal_token)
    if body.type != body.connection.type:
        raise HTTPException(422, "数据库类型不一致")
    try:
        value = connection_store.candidate(body.datasource_id, body.secret_ref, body.connection)
        connection_store.save(body.datasource_id, value)
        return connection_store.public(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/internal/v1/datasources/test-configuration")
def test_connection_configuration(body: ConfigureDataSource, x_semora_internal_token: str | None = Header(default=None)):
    require_internal_token(x_semora_internal_token)
    if body.type != body.connection.type:
        raise HTTPException(422, "数据库类型不一致")
    try:
        value = connection_store.candidate(body.datasource_id, body.secret_ref, body.connection)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if adapter_registry.is_jdbc(body.type):
        return jdbc_adapter.run(value, "test", timeout_ms=10000)
    inspect = mysql_adapter.inspect if adapter_registry.protocol(body.type)=="mysql" else inspect_connection
    return inspect(connection_store.dsn(value))


def inspect_connection(dsn: str) -> dict[str, Any]:
    try:
        with scan_control.track(psycopg.connect(dsn, autocommit=False, connect_timeout=5)) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT current_database(), current_user,
                    current_setting('default_transaction_read_only'),
                    rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls,
                    has_schema_privilege(current_user, 'public', 'CREATE')
                    FROM pg_roles WHERE rolname = current_user"""
                )
                row = cursor.fetchone()
                version = getattr(connection.info, "server_version", None)
                connection.rollback()
    except Exception as exc:
        raise database_error(exc, "test") from exc
    assert row is not None
    dangerous = any(bool(value) for value in row[3:8]) or bool(row[8])
    read_only = str(row[2]).lower() == "on" and not dangerous
    return {
        "product": "PostgreSQL",
        "driver":"psycopg",
        "driver_version":psycopg.__version__,
        "version": (f"{version // 10000}.{version % 10000}" if version >= 100000 else f"{version // 10000}.{version // 100 % 100}.{version % 100}") if version else "unknown",
        "database": row[0],
        "user": row[1],
        "default_transaction_read_only": row[2],
        "dangerous_role_capabilities": dangerous,
        "read_only": read_only,
    }


def sensitivity_level(table: str, column: str) -> str:
    """Scanning discovers structure; sensitivity is explicitly configured."""
    return "internal"


def scan_postgresql(dsn: str, selected_tables: list[str] | None = None, selected_schema: str | None = None) -> dict[str, Any]:
    connection_info = inspect_connection(dsn)
    if not connection_info["read_only"]:
        raise HTTPException(409, "数据源账号不是只读账号，拒绝扫描和注册")
    try:
        with scan_control.track(psycopg.connect(dsn, autocommit=False, connect_timeout=5)) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", (str(scan_timeout_ms()),))
                cursor.execute(
                    """SELECT table_schema,table_name,column_name,data_type,is_nullable,ordinal_position
                    FROM information_schema.columns
                    WHERE table_schema NOT IN ('pg_catalog','information_schema')
                    """ + (" AND (table_schema || '.' || table_name) = ANY(%s)" if selected_tables else "") + (" AND table_schema=%s" if selected_schema else "") + " ORDER BY table_schema,table_name,ordinal_position LIMIT 100001",
                    ([selected_tables] if selected_tables else []) + ([selected_schema] if selected_schema else [])
                )
                column_rows = cursor.fetchall()
                if len(column_rows)>100000: raise HTTPException(413,"扫描超过 10 万字段，请限定表范围")
                cursor.execute(
                    """SELECT n.nspname,c.relname,a.attname
                    FROM pg_constraint con
                    JOIN pg_class c ON c.oid=con.conrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY k(attnum,ord) ON true
                    JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum
                    WHERE con.contype='p'
                      AND n.nspname NOT IN ('pg_catalog','information_schema')
                      AND has_table_privilege(current_user,con.conrelid,'SELECT')
                    ORDER BY n.nspname,c.relname,k.ord"""
                )
                primary_keys = {(row[0], row[1], row[2]) for row in cursor.fetchall()}
                cursor.execute(
                    """SELECT ns.nspname,src.relname,sa.attname,
                    nt.nspname,tgt.relname,ta.attname
                    FROM pg_constraint con
                    JOIN pg_class src ON src.oid=con.conrelid
                    JOIN pg_namespace ns ON ns.oid=src.relnamespace
                    JOIN pg_class tgt ON tgt.oid=con.confrelid
                    JOIN pg_namespace nt ON nt.oid=tgt.relnamespace
                    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY sk(attnum,ord) ON true
                    JOIN LATERAL unnest(con.confkey) WITH ORDINALITY tk(attnum,ord) ON tk.ord=sk.ord
                    JOIN pg_attribute sa ON sa.attrelid=src.oid AND sa.attnum=sk.attnum
                    JOIN pg_attribute ta ON ta.attrelid=tgt.oid AND ta.attnum=tk.attnum
                    WHERE con.contype='f'
                      AND ns.nspname NOT IN ('pg_catalog','information_schema')
                      AND has_table_privilege(current_user,con.conrelid,'SELECT')
                      AND has_table_privilege(current_user,con.confrelid,'SELECT')
                    ORDER BY ns.nspname,src.relname,sk.ord"""
                )
                foreign_keys = [
                    {"from": f"{row[0]}.{row[1]}.{row[2]}", "to": f"{row[3]}.{row[4]}.{row[5]}"}
                    for row in cursor.fetchall()
                ]
                connection.rollback()
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error(exc, "scan") from exc
    tables: dict[tuple[str, str], dict[str, Any]] = {}
    sensitive_columns: list[dict[str, str]] = []
    for schema, table, column, data_type, nullable, _ in column_rows:
        value = tables.setdefault((schema, table), {"schema": schema, "name": table, "columns": []})
        level = sensitivity_level(table, column)
        value["columns"].append({
            "name": column,
            "type": data_type,
            "nullable": nullable == "YES",
            "primary_key": (schema, table, column) in primary_keys,
            "sensitivity": level,
        })
        if level in {"confidential", "restricted"}:
            sensitive_columns.append({"field": f"{schema}.{table}.{column}", "level": level})
    return {
        "status": "completed",
        "connection": connection_info,
        "tables": list(tables.values()),
        "foreign_keys": foreign_keys,
        "sensitive_columns": sensitive_columns,
    }


def assert_readonly(sql: str, dialect: str = "postgres") -> None:
    parser_sql = re.sub(r"%\([A-Za-z_][A-Za-z0-9_]*\)s", "1", sql)
    try:
        statements = sqlglot.parse(parser_sql, read=dialect)
    except sqlglot.errors.ParseError as exc:
        raise HTTPException(400, f"SQL 语法错误：{exc}") from exc
    if len(statements) != 1:
        raise HTTPException(400, "Executor 仅允许单条 SQL")
    ast = statements[0]
    if not isinstance(ast, (exp.Select, exp.Union, exp.Intersect, exp.Except)) or any(ast.find(node) for node in FORBIDDEN):
        raise HTTPException(400, "Executor 拒绝非只读 SQL")
    limit = ast.args.get("limit")
    where=ast.args.get("where")
    rownum_bound=(dialect=="oracle" and where is not None and isinstance(where.this,exp.LTE)
                 and isinstance(where.this.this,exp.Column) and where.this.this.name.upper()=="ROWNUM"
                 and isinstance(where.this.expression,exp.Literal) and where.this.expression.is_int)
    if limit is None and not rownum_bound:
        raise HTTPException(400, "Executor 要求显式 LIMIT")
    if ast.find(exp.Into) or ast.find(exp.Lock):
        raise HTTPException(400, "Executor 拒绝导出文件或锁定数据")
    functions = {func.sql_name().upper() if not isinstance(func, exp.Anonymous) else func.name.upper() for func in ast.find_all(exp.Func)}
    if functions & {"SLEEP", "BENCHMARK", "LOAD_FILE", "GET_LOCK", "RELEASE_LOCK"}:
        raise HTTPException(400, "Executor 拒绝有副作用的函数")


def mask_sensitive(column: str, value: Any, sensitive_columns: set[str] | None = None) -> Any:
    """Mask sensitive values before they can leave the isolated executor."""
    normalized = column.lower()
    configured_sensitive = {item.lower() for item in sensitive_columns or set()}
    if value is None or normalized not in configured_sensitive:
        return value.isoformat() if hasattr(value, "isoformat") else value
    text = str(value)
    return f"{text[:1]}{'*' * max(1, len(text) - 1)}"


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "semora-executor"}


@app.post("/internal/v1/datasources/test")
def test_datasource(body: DataSourceInput, x_semora_internal_token: str | None = Header(default=None)) -> dict:
    require_internal_token(x_semora_internal_token)
    if adapter_registry.is_jdbc(body.type):
        result = jdbc_adapter.run(jdbc_adapter.settings(body), "test", timeout_ms=10000, datasource_id=body.datasource_id)
    else:
        inspect = mysql_adapter.inspect if adapter_registry.protocol(body.type)=="mysql" else inspect_connection
        result = inspect(resolve_datasource(body))
    from .probe_identity import attach_identity
    return {"datasource_id": body.datasource_id, **attach_identity(result, body)}


@app.post("/internal/v1/datasources/scan")
def scan_datasource(body: DataSourceScanInput, x_semora_internal_token: str | None = Header(default=None)) -> dict:
    require_internal_token(x_semora_internal_token)
    with scan_control.operation(body.execution_id):
        if adapter_registry.is_jdbc(body.type):
            result = jdbc_adapter.run(jdbc_adapter.settings(body), "scan", tables=body.tables or [], execution_id=body.execution_id, timeout_ms=scan_timeout_ms(), datasource_id=body.datasource_id)
        elif adapter_registry.protocol(body.type)=="mysql":
            cfg = connection_store.get(body.datasource_id,body.secret_ref,body.type) or {}
            prefix = cfg.get("database", "") + "."
            table_names = [t[len(prefix):] if t.startswith(prefix) else t for t in body.tables] if body.tables else None
            result = mysql_adapter.scan(resolve_datasource(body), sensitivity_level, table_names)
        elif adapter_registry.protocol(body.type)=="postgres":
            cfg = connection_store.get(body.datasource_id,body.secret_ref,body.type) or {}
            result = scan_postgresql(resolve_datasource(body), body.tables, cfg.get("schema_name"))
            if body.tables:
                result["tables"] = [t for t in result["tables"] if f"{t['schema']}.{t['name']}" in body.tables]
        from .snapshots import normalize_snapshot, supplement_native
        if not adapter_registry.is_jdbc(body.type):
            result = supplement_native(result, resolve_datasource(body), body.type)
        cfg = connection_store.get(body.datasource_id,body.secret_ref,body.type) or {}
        if cfg.get("schema_name"):
            result["tables"]=[t for t in result.get("tables",[]) if t.get("schema")==cfg["schema_name"]]
        result = normalize_snapshot(result)
        complete = (not result.get('permission_gaps') and not result.get('errors') and result.get('partial') is not True
                    and result.get('status','completed')=='completed'
                    and all(t.get('columns') for t in result.get('tables',[])))
        result['status'] = 'completed' if complete else 'incomplete'
        result['scan_manifest'] = {'complete': complete, 'scope': 'selected' if body.tables else 'full',
                                   'scanned_tables': len(result.get('tables', []))}
        return {"datasource_id": body.datasource_id, **result}


@app.post("/internal/v1/query")
def execute_query(body: QueryInput, x_semora_internal_token: str | None = Header(default=None)) -> dict:
    require_internal_token(x_semora_internal_token)
    assert_readonly(body.sql, adapter_registry.dialect(body.datasource))
    timeout_ms = body.timeout_ms if body.timeout_ms is not None else query_timeout_ms()
    max_rows = body.max_rows if body.max_rows is not None else int(os.getenv("MAX_RESULT_ROWS", str(RESULT_DEFAULT_ROWS)))
    max_bytes = body.max_bytes if body.max_bytes is not None else int(os.getenv("MAX_RESULT_BYTES", str(RESULT_DEFAULT_MIB * MIB)))
    if adapter_registry.is_jdbc(body.datasource.type):
        cfg = jdbc_adapter.settings(body.datasource)
        sql, params = jdbc_adapter.bind_parameters(body.sql, body.parameters)
        result = jdbc_adapter.run(cfg, "query", sql=sql, parameters=params,
            execution_id=body.execution_id,
            timeout_ms=timeout_ms, max_rows=max_rows, max_bytes=max_bytes, datasource_id=body.datasource.datasource_id)
        result["rows"] = [[mask_sensitive(result["columns"][i], v, set(body.sensitive_columns)) for i,v in enumerate(row)] for row in result["rows"]]
        result["bytes_returned"] = len(json.dumps(result["rows"], ensure_ascii=False).encode())
        return result
    dsn = resolve_datasource(body.datasource)
    started = time.perf_counter()
    connection: psycopg.Connection | None = None
    native_protocol = adapter_registry.protocol(body.datasource.type)
    try:
        factory = (lambda: mysql_adapter.connect(dsn, timeout_ms)) if native_protocol=="mysql" else (lambda: psycopg.connect(dsn, autocommit=False))
        with factory() as connection:
            if body.execution_id:
                with ACTIVE_QUERIES_LOCK:
                    ACTIVE_QUERIES[body.execution_id] = connection
            with connection.cursor() as cursor:
                if native_protocol=="mysql":
                    mysql_adapter.begin_readonly(connection, timeout_ms)
                else:
                    cursor.execute("SET TRANSACTION READ ONLY")
                # PostgreSQL does not accept bind parameters in SET statements.
                # set_config keeps the timeout parameterized and transaction-local.
                if native_protocol!="mysql":
                    cursor.execute("SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",))
                cursor.execute(body.sql, body.parameters)
                columns = [column[0] for column in cursor.description or []]
                from .result_types import column_types, encode_scalar
                rows = cursor.fetchmany(max_rows + 1)
                result_types = column_types(cursor.description, native_protocol=="mysql", rows,
                    getattr(getattr(cursor,"_result",None),"fields",None))
                truncated = len(rows) > max_rows
                rows = rows[:max_rows]
                sensitive_columns = {item.lower() for item in body.sensitive_columns}
                serialized_rows = [
                    [encode_scalar(mask_sensitive(columns[index], value, sensitive_columns)) for index, value in enumerate(row)]
                    for row in rows
                ]
                payload_bytes = len(json.dumps(serialized_rows, ensure_ascii=False, default=str).encode())
                if payload_bytes > max_bytes:
                    raise HTTPException(413, f"查询结果超过 {max_bytes} 字节限制")
                connection.rollback()
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error(exc,'query') from exc
    finally:
        if body.execution_id:
            with ACTIVE_QUERIES_LOCK:
                if ACTIVE_QUERIES.get(body.execution_id) is connection:
                    ACTIVE_QUERIES.pop(body.execution_id, None)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return {
        "columns": columns,
        "column_types": result_types,
        "rows": serialized_rows,
        "row_count": len(serialized_rows),
        "duration_ms": duration_ms,
        "truncated": truncated,
        "bytes_returned": payload_bytes,
        "summary": {
            "timeout_ms": timeout_ms,
            "row_limit": max_rows,
            "transaction": "read_only",
            "masked_columns": [
                column
                for column in columns
                if column.lower() in sensitive_columns
            ],
        },
    }


@app.post("/internal/v1/executions/{execution_id}/cancel")
def cancel_query(execution_id: str, x_semora_internal_token: str | None = Header(default=None)) -> dict:
    require_internal_token(x_semora_internal_token)
    native_cancelled = scan_control.cancel(execution_id)
    if jdbc_adapter.cancel(execution_id) or native_cancelled:
        return {"execution_id": execution_id, "accepted": True}
    with ACTIVE_QUERIES_LOCK:
        connection = ACTIVE_QUERIES.get(execution_id)
    if connection is None:
        return {"execution_id": execution_id, "accepted": False, "reason": "not_active"}
    try:
        if hasattr(connection, "cancel"):
            connection.cancel()
        else:
            # PyMySQL has no cancellation API; closing the socket interrupts I/O.
            # Server-side MAX_EXECUTION_TIME bounds remaining server work.
            connection.close()
    except Exception as exc:
        raise HTTPException(502, f"取消数据库查询失败：{exc}") from exc
    return {"execution_id": execution_id, "accepted": True}


@app.post("/internal/v1/estimate")
def estimate_query(body: EstimateInput, x_semora_internal_token: str | None = Header(default=None)) -> dict:
    """Estimate a read-only query without executing it or returning business data."""
    require_internal_token(x_semora_internal_token)
    assert_readonly(body.sql, adapter_registry.dialect(body.datasource))
    if adapter_registry.is_jdbc(body.datasource.type):
        # Authentication/permissions must be checked before reporting preflight unavailable.
        result = jdbc_adapter.run(jdbc_adapter.settings(body.datasource), "test", timeout_ms=body.timeout_ms, datasource_id=body.datasource.datasource_id)
        if not result.get("read_only"):
            raise HTTPException(409, detail={"error_type": "permission", "message": "尚未通过只读验证"})
        raise HTTPException(422, detail={"error_type": "capability", "operation": "estimate", "message": "该 JDBC 来源不支持无副作用预检，请告警后实际只读执行"})
    dsn = resolve_datasource(body.datasource)
    if adapter_registry.protocol(body.datasource.type)=="mysql":
        return mysql_adapter.estimate(dsn, body.sql, body.parameters, body.timeout_ms)
    try:
        with psycopg.connect(dsn, autocommit=False) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", (f"{body.timeout_ms}ms",))
                cursor.execute(f"EXPLAIN (FORMAT JSON) {body.sql}", body.parameters)
                payload = cursor.fetchone()
                connection.rollback()
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error(exc,'estimate') from exc
    if not payload:
        raise HTTPException(502, "查询成本估算未返回执行计划")
    document = payload[0]
    if isinstance(document, str):
        document = json.loads(document)
    plan = document[0]["Plan"]
    return {
        "estimated_rows": int(plan.get("Plan Rows", 0)),
        "total_cost": float(plan.get("Total Cost", 0)),
        "source": "postgresql-explain",
    }



@app.get("/internal/v1/database-drivers")
def database_drivers(x_semora_internal_token: str | None = Header(default=None)):
    require_internal_token(x_semora_internal_token)
    return drivers.list_drivers()

@app.post("/internal/v1/database-drivers")
async def upload_driver(config: str = Form(...), files: list[UploadFile] = File(...), x_semora_internal_token: str | None = Header(default=None)):
    require_internal_token(x_semora_internal_token)
    try:
        settings = drivers.DriverConfig.model_validate_json(config)
    except ValueError:
        raise HTTPException(422, "驱动配置格式无效") from None
    payload=[]; total=0
    for file in files:
        data=await file.read(100*1024**2+1);total+=len(data)
        if total>100*1024**2: raise HTTPException(413, "驱动包超过 100MB")
        payload.append((file.filename or "",data))
    return drivers.install(settings, payload)

@app.put("/internal/v1/database-drivers/{driver_id}")
def update_driver(driver_id: str, body: drivers.DriverConfig, x_semora_internal_token: str | None = Header(default=None)):
    require_internal_token(x_semora_internal_token)
    return drivers.update(driver_id, body)


@app.post("/internal/v1/datasources/validate-query")
def validate_datasource_query(body: DataSourceValidationInput, x_semora_internal_token: str | None = Header(default=None)):
    require_internal_token(x_semora_internal_token)
    if adapter_registry.is_jdbc(body.type):
        from .probe_identity import attach_identity
        return attach_identity(jdbc_adapter.validate(body, body.probe_keys), body)
    execute_query(QueryInput(sql="SELECT 1 AS semora_probe LIMIT 1",datasource=body,timeout_ms=5000,max_rows=1),x_semora_internal_token)
    result=test_datasource(body,x_semora_internal_token)
    from .sql_capabilities import probe_native
    try:probes=probe_native(body.type,resolve_datasource(body), body.probe_keys)
    except Exception as exc:raise database_error(exc,"test") from exc
    result["probes"]={k:v for k,v in probes.items() if k!="sql_baseline"}
    result["capabilities"]={"connection":"verified","query":"verified","dialect":adapter_registry.dialect(body),"live_validation":"synthetic_query_only",**probes}
    return result


@app.get("/internal/v1/executions/{execution_id}/progress")
def execution_progress(execution_id: str, x_semora_internal_token: str | None = Header(default=None)):
    require_internal_token(x_semora_internal_token)
    return jdbc_adapter.progress(execution_id)


class ProfileGuard(BaseModel):
    side: Literal["source", "target"]
    field: str = Field(min_length=1, max_length=255)
    operator: Literal["eq"] = "eq"
    value: str | int | float | bool


class ProfileInput(BaseModel):
    datasource: DataSourceInput
    target_datasource: DataSourceInput | None = None
    source_table: str = Field(min_length=1,max_length=800)
    source_columns: list[str] = Field(min_length=1,max_length=8)
    target_table: str = Field(min_length=1,max_length=800)
    target_columns: list[str] = Field(min_length=1,max_length=8)
    sample_rows: int = Field(default=100,ge=10,le=500)
    source_index_confirmed: bool = False
    target_unique_index_confirmed: bool = False
    allow_bounded_unindexed: bool = False
    guards: list[ProfileGuard] = Field(default_factory=list, max_length=8)
    target_context_enums: dict[str, list[int]] = Field(default_factory=dict)
    source_sampling_key: str | None = Field(default=None, min_length=1, max_length=255)
    target_sampling_key: str | None = Field(default=None, min_length=1, max_length=255)


def _directional_relationship_profile(body: ProfileInput, x_semora_internal_token=None, *, profile_id=None, direction='forward'):
    require_internal_token(x_semora_internal_token)
    if len(body.source_columns)!=len(body.target_columns):raise HTTPException(422,"复合键字段数不一致")
    if len(body.target_context_enums)>8 or any(not 2 <= len(v) <= 16 for v in body.target_context_enums.values()):
        raise HTTPException(422, 'Enumeration context exceeds the auxiliary statistics budget')
    from uuid import uuid4
    import logging
    profile_id = profile_id or uuid4().hex[:12]
    timeout = relationship_profile_timeout_ms()
    sampling_method = 'database_random_distinct_keys'

    def run(tree, parameters, stage, max_rows, datasource):
        dialect = adapter_registry.dialect(datasource)
        sql = tree.sql(dialect=dialect)
        for name in sorted(parameters, key=len, reverse=True):
            sql = sql.replace(f':{name}', f'%({name})s')
        context = dict(event='relationship.profile.query', profile_id=profile_id,
            direction=direction, stage=stage, datasource_id=datasource.datasource_id,
            source_table=body.source_table, target_table=body.target_table,
            source_columns=body.source_columns, target_columns=body.target_columns,
            sql=sql, parameter_count=len(parameters), timeout_ms=timeout)
        logger = logging.getLogger('uvicorn.error')
        logger.info(json.dumps({**context, 'status':'started'}, ensure_ascii=False))
        started = time.monotonic()
        try:
            result = execute_query(QueryInput(sql=sql,parameters=parameters,datasource=datasource,
                timeout_ms=timeout,max_rows=max_rows,max_bytes=1024*1024),x_semora_internal_token)
        except Exception as exc:
            detail = getattr(exc, 'detail', {})
            logger.warning(json.dumps({**context,'status':'failed',
                'duration_ms':round((time.monotonic()-started)*1000),
                'error_type':type(exc).__name__,
                'db_error_code':detail.get('db_error_code') if isinstance(detail,dict) else None,
                'timeout_origin':detail.get('diagnostic',{}).get('timeout_origin') if isinstance(detail,dict) else None},ensure_ascii=False))
            raise
        logger.info(json.dumps({**context,'status':'completed','row_count':len(result['rows']),
            'duration_ms':round((time.monotonic()-started)*1000)},ensure_ascii=False))
        return result

    def table_expr(table):
        parts=table.split(".")
        if any(not p or len(p)>255 for p in parts):raise HTTPException(422,"表标识无效")
        return exp.Table(
            this=exp.to_identifier(parts[-1],quoted=True),
            db=exp.to_identifier(parts[-2],quoted=True) if len(parts)>1 else None,
            catalog=exp.to_identifier(parts[-3],quoted=True) if len(parts)>2 else None)

    def run_sample(datasource, table, columns):
        nonlocal sampling_method
        dialect=adapter_registry.dialect(datasource)
        tree=exp.select(*(exp.column(c,quoted=True) for c in columns)).from_(table_expr(table))
        tree=tree.where(exp.and_(*(exp.column(c,quoted=True).is_(exp.null()).not_() for c in columns)))
        parameters = {}
        source_guards = [g for g in body.guards if g.side == 'source']
        for index, guard in enumerate(source_guards):
            name = f"source_guard_{index}"
            parameters[name] = guard.value
            tree = tree.where(exp.EQ(this=exp.column(guard.field, quoted=True), expression=exp.Placeholder(this=name)))
        if body.source_sampling_key and dialect in {'mysql', 'postgres'}:
            from .relationship_sampling import indexed_sample
            rows, exhausted = indexed_sample(table_expr(table), columns, body.source_sampling_key,
                dialect, source_guards, parameters, body.sample_rows,
                lambda t,p,s,n:run(t,p,s,n,datasource))
            sampling_method = 'approximate_random_index_windows'
            return rows, exhausted
        # Randomize DISTINCT complete keys, not rows: repeated values must not
        # dominate a sample. SQL Server RAND() is constant per statement.
        random_order = {'mysql': exp.Rand(), 'postgres': exp.func('RANDOM'),
                        'tsql': exp.func('NEWID'),
                        'oracle': exp.Dot(this=exp.to_identifier('DBMS_RANDOM'), expression=exp.to_identifier('VALUE'))}
        if dialect not in random_order:
            raise HTTPException(422,'当前数据库没有可用的随机采样方言')
        tree=exp.select(*(exp.column(c,quoted=True) for c in columns)).from_(tree.distinct().subquery('sample_keys'))
        tree=tree.order_by(random_order[dialect])
        tree=tree.limit(min(body.sample_rows + 1, 1000))
        from semora_contracts.sql_compat import apply_pagination
        pagination="baseline"
        if adapter_registry.is_jdbc(datasource.type):
            driver,_,_=jdbc_adapter.configuration(jdbc_adapter.settings(datasource))
            pagination=driver["effective_pagination"]
        tree=apply_pagination(tree,dialect,pagination)
        result=run(tree, parameters, 'sampling_distinct_random', min(body.sample_rows + 1, 1000), datasource)
        return result['rows'], len(result['rows']) <= body.sample_rows

    rows, source_exhausted = run_sample(body.datasource,body.source_table,body.source_columns)
    window = [tuple(json.dumps(v,sort_keys=True,default=str) for v in row)
              for row in rows if all(v is not None for v in row)]
    keys=list(dict.fromkeys(window))[:body.sample_rows]
    target=body.target_datasource or body.datasource
    matched=set()
    context_counts = {}
    context_errors = []
    multiplicities = []
    dialect=adapter_registry.dialect(target)
    batch_size=100 if len(body.target_columns)>1 else 200
    for start in range(0,len(keys),batch_size):
        batch=keys[start:start+batch_size]
        parameters={};disjunction=[]
        for row_index,row in enumerate(batch):
            conjunction=[]
            for column_index,(column,value) in enumerate(zip(body.target_columns,row)):
                name=f"k_{row_index}_{column_index}";parameters[name]=json.loads(value)
                conjunction.append(exp.EQ(this=exp.column(column,quoted=True),expression=exp.Placeholder(this=name)))
            disjunction.append(exp.and_(*conjunction))
        tree=exp.select(*(exp.column(c,quoted=True) for c in body.target_columns), exp.Count(this=exp.Star()).as_('matching_rows')).from_(table_expr(body.target_table))
        tree=tree.group_by(*(exp.column(c,quoted=True) for c in body.target_columns))
        predicate = (exp.column(body.target_columns[0],quoted=True).isin(*(
            exp.Placeholder(this=f'k_{i}_0') for i in range(len(batch))))
            if len(body.target_columns)==1 else exp.or_(*disjunction))
        tree=tree.where(predicate).limit(len(batch))
        for index, guard in enumerate(body.guards):
            if guard.side == "target":
                name = f"target_guard_{index}"
                parameters[name] = guard.value
                tree = tree.where(exp.EQ(this=exp.column(guard.field, quoted=True), expression=exp.Placeholder(this=name)))
        result=run(tree,parameters,'matching_keys',max(1,len(batch)),target)
        matched.update(tuple(json.dumps(v,sort_keys=True,default=str) for v in row[:len(body.target_columns)]) for row in result["rows"])
        multiplicities.extend(int(row[len(body.target_columns)]) if len(row)>len(body.target_columns) else 1 for row in result['rows'])
        if result['rows']:
            for field, values in body.target_context_enums.items():
                # Project only codes already declared in schema comments, never arbitrary attribute values.
                category = exp.Case(ifs=[exp.If(this=exp.EQ(this=exp.column(field, quoted=True),
                    expression=exp.Literal.number(v)), true=exp.Literal.string(str(v))) for v in values],
                    default=exp.Literal.string('unlisted'))
                aggregate = exp.select(category.copy().as_('category'), exp.Count(this=exp.Star()).as_('row_count'))
                aggregate = aggregate.from_(table_expr(body.target_table)).where(tree.args['where'].this.copy())
                aggregate = aggregate.group_by(category).limit(len(values)+1)
                try:
                    details = run(aggregate,parameters,'matching_enum_context',len(values)+1,target)
                    counts = context_counts.setdefault(field, {})
                    for code, count in details['rows']:
                        label = str(code)
                        counts[label] = counts.get(label, 0) + int(count)
                except Exception as exc:
                    context_errors.append({'field': field, 'error_type': type(exc).__name__})
    secret=os.getenv("RELATIONSHIP_PROFILE_HMAC_KEY",os.getenv("INTERNAL_TOKEN","semora-dev-internal")).encode()
    signatures=sorted(hmac.new(secret,"\x1f".join(row).encode(),hashlib.sha256).hexdigest()[:24] for row in keys)[:64]
    return {"method":"random_directional_membership_v4","sample_budget":body.sample_rows,
            "sampling_unit":"distinct_keys", "sampling_method":sampling_method,
            "sampling_scope":"bounded_index_windows" if sampling_method=='approximate_random_index_windows' else 'all_distinct_keys',
            "uniform_distinct_key_sample":sampling_method=='database_random_distinct_keys',
            "target_context": context_counts, "target_context_errors": context_errors,
            "source_exhausted":source_exhausted,
            "guards_applied":[g.model_dump() for g in body.guards],
            "source_nonnull_distinct_keys":len(keys),"matched_keys":len(set(keys)&matched),
            "directional_coverage":round(len(set(keys)&matched)/len(keys),6) if keys else 0,
            "matched_row_count":sum(multiplicities), "max_matches_per_key":max(multiplicities,default=0),
            "duplicate_matched_keys":sum(n>1 for n in multiplicities),
            "target_uniqueness":"declared_constraint" if body.target_unique_index_confirmed else "unverified",
            "index_verified_by_metadata":body.source_index_confirmed and body.target_unique_index_confirmed,
            "signature_version":"hmac-sha256-v1","signature":signatures,
            "raw_values_retained":False}


@app.post("/internal/v1/datasources/profile-relationship")
def profile_relationship(body: ProfileInput, x_semora_internal_token: str | None = Header(default=None)):
    require_internal_token(x_semora_internal_token)
    reverse_body = body.model_copy(update={
        'datasource': body.target_datasource or body.datasource, 'target_datasource': body.datasource,
        'source_table': body.target_table, 'target_table': body.source_table,
        'source_columns': body.target_columns, 'target_columns': body.source_columns,
        'source_index_confirmed': body.target_unique_index_confirmed,
        'target_unique_index_confirmed': False,
        'source_sampling_key':body.target_sampling_key, 'target_sampling_key':body.source_sampling_key,
        'guards': [g.model_copy(update={'side':'target' if g.side=='source' else 'source'}) for g in body.guards],
        'target_context_enums': {}})
    from uuid import uuid4
    profile_id = uuid4().hex[:12]
    results=[]
    for direction, value in [('forward',body),('reverse',reverse_body)]:
        try:
            results.append({**_directional_relationship_profile(value,x_semora_internal_token,
                profile_id=profile_id,direction=direction),'status':'available'})
        except Exception as exc:
            import logging
            detail=getattr(exc,'detail',{})
            code=detail.get('db_error_code') if isinstance(detail,dict) else None
            logging.getLogger('semora.executor').warning('relationship.profile.direction_unavailable profile_id=%s direction=%s source=%s target=%s error_type=%s db_error_code=%s',
                profile_id,direction,value.source_table,value.target_table,type(exc).__name__,code)
            results.append({'status':'unavailable','error_type':type(exc).__name__,'db_error_code':code,
                'db_error_type':detail.get('error_type') if isinstance(detail,dict) else None,
                'timeout_origin':detail.get('diagnostic',{}).get('timeout_origin') if isinstance(detail,dict) else None,
                'source_nonnull_distinct_keys':0,'matched_keys':0,'directional_coverage':0,'source_exhausted':False})
    forward,reverse=results
    if all(r['status']=='unavailable' for r in results):
        raise HTTPException(503,{'message':'双向随机采样均未取得数据证据，请查看验证日志',
            'profile_id':profile_id,'directions':{'forward':forward,'reverse':reverse}})
    return {**forward, 'method':'bidirectional_random_membership_v4',
            'sample_budget':body.sample_rows,'sampling_unit':'distinct_keys',
            'sampling_method':'independent_directional_sampling','profile_id':profile_id,'raw_values_retained':False,
            'forward':{k:v for k,v in forward.items() if k!='signature'},
            'reverse':{k:v for k,v in reverse.items() if k!='signature'},
            'reverse_directional_coverage':reverse['directional_coverage'],
            'target_nonnull_distinct_keys':reverse['source_nonnull_distinct_keys'],
            'reverse_matched_keys':reverse['matched_keys'],
            'target_exhausted':reverse['source_exhausted']}


class RelationDefinitionInput(DataSourceInput):
    table_id: str = Field(min_length=1, max_length=800)


@app.post("/internal/v1/datasources/relation-definitions")
def relation_definitions(body: RelationDefinitionInput, x_semora_internal_token: str | None = Header(default=None)):
    require_internal_token(x_semora_internal_token)
    if adapter_registry.protocol(body.type)=="mysql":
        parts = body.table_id.split(".")
        if len(parts) != 2:
            raise HTTPException(422, "视图标识需包含数据库及名称")
        sql = "SELECT VIEW_DEFINITION FROM information_schema.VIEWS WHERE TABLE_SCHEMA=%(schema)s AND TABLE_NAME=%(name)s LIMIT 1"
        parameters = {"schema": parts[0], "name": parts[1]}
    elif adapter_registry.protocol(body.type)=="postgres":
        sql = "SELECT pg_get_viewdef(to_regclass(%(table)s), true) LIMIT 1"
        parameters = {"table": body.table_id}
    else:
        return {"status": "unavailable", "reason": "driver_definition_reader_unavailable"}
    result = execute_query(QueryInput(datasource=body, sql=sql, parameters=parameters,
                           timeout_ms=10000, max_rows=1, max_bytes=48000), x_semora_internal_token)
    return {"status": "available" if result["rows"] else "unavailable",
            "definition": result["rows"][0][0] if result["rows"] else None}
