"""Isolated, bounded DuckDB evaluation of previously retrieved result tables."""

from decimal import Decimal
import json
import re
import threading
import time
from semora_contracts import ExecutionResult
from .sql_errors import SQLIssue


def _type(type_name):
    """Types originate in cursor metadata, never in model-authored DDL."""
    name = str(type_name).upper()
    name={"TIMESTAMP WITH TIME ZONE":"TIMESTAMPTZ","TIME WITH TIME ZONE":"TIMETZ"}.get(name,name)
    if re.fullmatch(r"DECIMAL\([1-9][0-9]?,[0-9]+\)", name):
        return name
    if name in {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UBIGINT",
        "DOUBLE",
        "FLOAT",
        "BOOLEAN",
        "DATE",
        "TIME",
        "TIMESTAMP",
        "TIMESTAMPTZ",
        "TIMETZ",
        "VARCHAR",
    }:
        return name
    return "VARCHAR"


def local_query(artifact, results, settings):
    import duckdb

    started = time.perf_counter()
    con = duckdb.connect(
        ":memory:",
        config={
            "enable_external_access": "false",
            "threads": "1",
            "memory_limit": "256MB",
            "max_temp_directory_size": "0B",
        },
    )
    timed_out = threading.Event()
    def interrupt():
        timed_out.set()
        con.interrupt()
    timer = threading.Timer(float(settings.get("query_timeout_seconds", 30)), interrupt)
    try:
        timer.start()
        for name in artifact.compilation.get("local_dependencies", list(results)):
            result = results[name]
            if result.truncated:
                raise SQLIssue("前置结果不完整，不能继续本地关联", kind="incomplete")
            if len(set(result.columns)) != len(result.columns):
                raise SQLIssue("前置输出列重名，请为每个输出指定唯一别名")
            quote = lambda s: '"' + s.replace('"', '""') + '"'
            types = result.column_types or ["VARCHAR"] * len(result.columns)
            if len(types) != len(result.columns):
                raise SQLIssue("前置输出类型契约不完整")
            ddl = ",".join(quote(c) + " " + _type(t) for c, t in zip(result.columns, types))
            con.execute("CREATE TABLE " + quote(name) + " (" + ddl + ")")
            if result.rows:
                con.executemany(
                    "INSERT INTO "
                    + quote(name)
                    + " VALUES ("
                    + ",".join("?" for _ in result.columns)
                    + ")",
                    result.rows,
                )
        if timed_out.is_set():
            from .executor_errors import ExecutorOperationError
            raise ExecutorOperationError('timeout', diagnostic={'timeout_origin':'system'})
        con.execute("EXPLAIN " + artifact.physical_sql, artifact.parameters)
        cursor = con.execute(artifact.physical_sql, artifact.parameters)
        limit = int(settings.get("max_result_rows", 1000))
        rows = cursor.fetchmany(limit + 1)
        serialized = [
            [
                str(v)
                if isinstance(v, Decimal)
                else v.isoformat()
                if hasattr(v, "isoformat")
                else v
                for v in row
            ]
            for row in rows[:limit]
        ]
        size = len(json.dumps(serialized, default=str, ensure_ascii=False).encode())
        if size > int(settings.get("max_result_mb", 10)) * 1024 * 1024:
            raise SQLIssue("本地查询结果超过大小限制", kind="incomplete")
        if timed_out.is_set():
            from .executor_errors import ExecutorOperationError
            raise ExecutorOperationError('timeout', diagnostic={'timeout_origin':'system'})
        return ExecutionResult(
            columns=[d[0] for d in cursor.description],
            column_types=[str(d[1]) for d in cursor.description],
            rows=serialized,
            row_count=len(serialized),
            duration_ms=int((time.perf_counter() - started) * 1000),
            truncated=len(rows) > limit,
            bytes_returned=size,
        )
    except Exception:
        if timed_out.is_set():
            from .executor_errors import ExecutorOperationError
            raise ExecutorOperationError('timeout', diagnostic={'timeout_origin':'system'}) from None
        raise
    finally:
        timer.cancel()
        con.close()
