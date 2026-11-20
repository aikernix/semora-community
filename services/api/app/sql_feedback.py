"""Persist concrete SQL rejection evidence without guessing global capabilities."""
import hashlib
from datetime import datetime, timezone

import sqlglot
from .sql_dialect import parse_query
from sqlglot import exp

from .datasources import datasource_service
from .executor_errors import ExecutorOperationError


def record_sql_rejection(step, artifact, error, stage):
    if step.engine != "source" or not isinstance(error, ExecutorOperationError):
        return None
    missing_function = error.kind == "schema" and error.code in {1305, "42883"}
    if error.kind not in {"syntax", "unsupported_sql"} and not missing_function:
        return None
    source = datasource_service.repository.get(step.data_source_id)
    functions, features = [], []
    try:
        tree = parse_query(step.sql, dialect=getattr(step, "dialect", "") or "postgres")
        functions = sorted({f.name.upper() if isinstance(f, exp.Anonymous) else f.sql_name() for f in tree.find_all(exp.Func)})[:16]
        if tree.find(exp.Window): features.append("window_functions")
        if tree.find(exp.With): features.append("cte")
        if any(w.args.get("recursive") for w in tree.find_all(exp.With)): features.append("recursive_cte")
        if tree.find(exp.Offset): features.append("offset_pagination")
    except (ValueError, sqlglot.errors.SqlglotError):
        pass
    # A rejected complex statement cannot prove that every feature it uses is
    # unavailable. Only isolated capability probes set global unsupported flags.
    entry = {
        "scope": "statement_only", "error_type": error.kind, "db_error_code": error.code,
        "sql_hash": hashlib.sha256(artifact.physical_sql.encode()).hexdigest(),
        "features_used": features, "functions_used": functions,
        "error_stage": stage, "diagnostic": dict(error.diagnostic),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    previous = source.scan_result.get("sql_feedback") or []
    source.scan_result["sql_feedback"] = [
        item for item in previous
        if (item.get("sql_hash"), item.get("error_type"), item.get("db_error_code")) !=
           (entry["sql_hash"], entry["error_type"], entry["db_error_code"])
    ][-31:] + [entry]
    datasource_service.repository.save(source)
    return entry
