"""Learn observed SQL forms after actual rows return, without generalizing all argument types."""
from datetime import datetime, timezone
import hashlib
import logging

import sqlglot
from .sql_dialect import parse_query
from sqlglot import exp
from .datasources import datasource_service

LOG = logging.getLogger(__name__)


def record_sql_success(step, result):
    if step.engine != "source" or result.row_count == 0:
        return
    source = datasource_service.repository.get(step.data_source_id)
    tree = parse_query(step.sql, dialect=getattr(step, "dialect", "") or "postgres")
    observed = {}
    for select in ([tree] if isinstance(tree, exp.Select) else []):
        for expression in select.expressions:
            root = expression.this if isinstance(expression, exp.Alias) else expression
            node = root.this if isinstance(root, exp.Window) else root
            if not isinstance(node, exp.Func): continue
            function = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            signature = hashlib.sha256(root.sql(dialect="postgres").encode()).hexdigest()[:24]
            kinds = sorted({"column_unknown" if isinstance(n, exp.Column) else "parameter_unknown" if isinstance(n, exp.Placeholder) else "text_literal" if n.is_string else "number_literal"
                            for n in root.walk() if isinstance(n, (exp.Column, exp.Placeholder, exp.Literal))})
            observed["observed:" + signature] = {"status": "verified", "method": "successful_query",
                "scope": "observed_expression", "function": function, "argument_kinds": kinds,
                "context": "window" if isinstance(root, exp.Window) else "aggregate" if isinstance(node, exp.AggFunc) else "scalar",
                "observed_at": datetime.now(timezone.utc).isoformat()}
    if not observed: return
    previous = source.scan_result.get("sql_observations", {})
    entries = {**previous, **observed}
    source.scan_result["sql_observations"] = dict(sorted(entries.items(), key=lambda pair: pair[1]["observed_at"])[-64:])
    datasource_service.repository.save(source)
    try:
        datasource_service.repository.capability_cache.publish(source.type, source.scan_result.get("connection", {}),
            source.scan_result.get("capabilities", {}), source.id, observed)
    except Exception as exc:
        LOG.warning("SQL capability learning cache unavailable: %s", type(exc).__name__)
