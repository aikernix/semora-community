"""Versioned, synthetic read-only probe signatures. No business tables or values."""
from decimal import Decimal

import sqlglot
from sqlglot import exp

PROBE_CATALOG_VERSION = "common-read-v1"
SMOKE_PROBES = ("pagination", "types", "fn.abs.integer", "fn.coalesce.integer")


def probe_catalog(dialect, pagination="baseline"):
    entries = {
        "cte": ("WITH semora_probe AS (SELECT 1 AS n) SELECT n FROM semora_probe", 1),
        "window_functions": ("SELECT ROW_NUMBER() OVER (ORDER BY n) FROM (SELECT 1 AS n) semora_probe", 1),
        "recursive_cte": ("WITH RECURSIVE semora_probe(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM semora_probe WHERE n<2) SELECT MAX(n) FROM semora_probe", 2),
        "pagination": ("SELECT 1 AS n LIMIT 1", 1),
        "offset_pagination": ("SELECT n FROM (SELECT 1 AS n UNION ALL SELECT 2 AS n) semora_probe ORDER BY n LIMIT 1 OFFSET 1", 2),
        "types": ("SELECT CAST(123.45 AS DECIMAL(18,2)),CAST(NULL AS VARCHAR(10))", "123.45"),
        "fn.abs.integer": ("SELECT ABS(-5)", 5),
        "fn.round.decimal": ("SELECT ROUND(CAST(1.25 AS DECIMAL(4,2)),1)", "1.3"),
        "fn.coalesce.integer": ("SELECT COALESCE(CAST(NULL AS INTEGER),2)", 2),
        "fn.nullif.integer": ("SELECT NULLIF(1,1)", None),
        "fn.upper.text": ("SELECT UPPER('ab')", "AB"),
        "fn.lower.text": ("SELECT LOWER('AB')", "ab"),
        "fn.length.text": ("SELECT LENGTH('abc')", 3),
        "fn.substring.text": ("SELECT SUBSTRING('abc' FROM 2 FOR 1)", "b"),
        "fn.concat.text": ("SELECT CONCAT('a','b')", "ab"),
        "fn.trim.text": ("SELECT TRIM(' a ')", "a"),
        "fn.mod.integer": ("SELECT MOD(5,2)", 1),
        "fn.power.integer": ("SELECT POWER(2,3)", 8),
        "fn.cast.integer": ("SELECT CAST('12' AS INTEGER)", 12),
        "fn.sum.integer": ("SELECT SUM(n) FROM (SELECT 1 AS n UNION ALL SELECT 2 AS n) semora_probe", 3),
        "fn.avg.integer": ("SELECT AVG(n) FROM (SELECT 1 AS n UNION ALL SELECT 3 AS n) semora_probe", 2),
        "fn.count.integer": ("SELECT COUNT(*) FROM (SELECT 1 AS n UNION ALL SELECT 2 AS n) semora_probe", 2),
        "fn.extract_year.date": ("SELECT EXTRACT(YEAR FROM DATE '2024-01-02')", 2024),
        "fn.extract_month.date": ("SELECT EXTRACT(MONTH FROM DATE '2024-01-02')", 1),
    }
    result = {}
    for key, (baseline, expected) in entries.items():
        numeric = isinstance(expected, (int, float)) or key in {"types", "fn.round.decimal"}
        spec = {"signature": key, "scope": "synthetic_signature", "expected_kind": "null" if expected is None else "number" if numeric else "text", "expected": None if expected is None else str(expected)}
        try:
            tree = sqlglot.parse_one(baseline, read="postgres")
            if dialect == "oracle":
                tree = tree.transform(lambda n: exp.Anonymous(this="TO_DATE", expressions=[n.this.copy(), exp.Literal.string("YYYY-MM-DD")])
                    if isinstance(n, exp.Cast) and n.to.this == exp.DataType.Type.DATE and isinstance(n.this, exp.Literal) and n.this.is_string else n)
                for select in tree.find_all(exp.Select):
                    if select.args.get("from") is None:
                        select.set("from", exp.From(this=exp.Table(this=exp.to_identifier("DUAL"))))
                if key == "pagination":
                    from .sql_compat import apply_pagination
                    tree = apply_pagination(tree, dialect, pagination)
            spec["sql"] = tree.sql(dialect=dialect, unsupported_level=sqlglot.ErrorLevel.RAISE)
        except (ValueError, sqlglot.errors.SqlglotError):
            spec["conversion_unavailable"] = True
        result[key] = spec
    return result


def probe_matches(row, spec):
    if not row:
        return False
    value = row[0]
    if spec["expected_kind"] == "null":
        return value is None
    if value is None:
        return False
    if spec["expected_kind"] == "number":
        try:
            return Decimal(str(value)) == Decimal(spec["expected"]) and (spec["signature"] != "types" or len(row) > 1 and row[1] is None)
        except (ValueError, ArithmeticError):
            return False
    return str(value) == spec["expected"]
