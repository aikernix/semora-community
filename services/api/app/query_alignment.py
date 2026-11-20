"""Deterministic checks between the internal logical plan and authored SQL.

These checks only cover facts that can be proved from published mappings and the
SQL AST.  They deliberately do not attempt to interpret free-form business text.
"""
from __future__ import annotations

from typing import Any

import sqlglot
from .sql_dialect import parse_query
from sqlglot import exp


def _catalog(materials: dict[str, Any]):
    objects, fields = {}, {}
    for item in materials.get("objects", []):
        obj = item.get("object", item)
        name = obj.get("name")
        if not name:
            continue
        objects[name] = obj
        for prop in obj.get("properties", []):
            if prop.get("name"):
                fields[f"{name}.{prop['name']}"] = {
                    "table": obj.get("table", ""),
                    "column": prop.get("column") or prop["name"],
                }
    relations = {
        rel.get("name"): rel for rel in materials.get("relations", []) if rel.get("name")
    }
    return objects, fields, relations


def validate_logical_refs(plan, materials: dict[str, Any]) -> list[str]:
    """Return precise invalid-reference diagnostics without guessing replacements."""
    if plan is None:
        return []
    objects, fields, relations = _catalog(materials)
    errors = []
    for ref in plan.subject_refs:
        if ref not in objects:
            errors.append(f"未知对象引用：{ref}")
    metric_ids={metric.id for metric in plan.metrics}
    # Output may refer to an explicitly declared calculation. Its source
    # fields are still checked below; this is not an arbitrary unknown alias.
    field_refs = [*[ref for ref in plan.output_refs if ref not in metric_ids], *plan.dimension_refs]
    for metric in plan.metrics:
        field_refs.extend(metric.field_refs)
    for item in [*plan.filters,*plan.join_filters]:
        field_refs.extend(item.field_refs)
        if item.relation_ref and item.relation_ref not in relations:
            errors.append(f"未知关系引用：{item.relation_ref}")
    for item in plan.sort:
        if "." in item.target_ref:
            field_refs.append(item.target_ref)
    for ref in field_refs:
        if ref not in fields:
            errors.append(f"未知属性引用：{ref}")
    for ref in plan.relation_refs:
        if ref not in relations:
            errors.append(f"未知关系引用：{ref}")
    return list(dict.fromkeys(errors))


def validate_required_object_gaps(report, materials: dict[str, Any]) -> list[str]:
    """Check structured necessity claims, without interpreting business keywords."""
    objects, fields, _ = _catalog(materials)
    required = {use.object for use in report.required_object_uses}
    errors = [f"必需对象缺少已加载资料：{name}" for name in sorted(required - objects.keys())]
    for gap in report.technical_gaps:
        if gap.kind != "relation":
            continue
        endpoints = set(gap.objects)
        if len(endpoints) < 2:
            errors.append("关联缺口必须声明至少两个当前查询必需对象")
        if endpoints - required:
            errors.append("关联缺口涉及未证明必要性的候选对象：" + ", ".join(sorted(endpoints - required)))
        for ref in gap.field_refs:
            if ref not in fields or ref.rsplit(".", 1)[0] not in endpoints:
                errors.append(f"关联缺口属性必须来自所声明对象的已有资料：{ref}")
    return list(dict.fromkeys(errors))


def _column_names(node, aliases):
    values = set()
    for col in node.find_all(exp.Column):
        column = col.name.lower()
        table = aliases.get(col.table.lower(), col.table.lower()) if col.table else ""
        values.add((table, column))
        values.add(("", column))
    return values


def _tree_facts(program):
    columns, grouped, filtered = set(), set(), set()
    aggregates, has_order, limits = [], False, []
    for step in program.steps:
        tree = parse_query(step.sql, dialect=step.dialect or "postgres")
        aliases = {t.alias_or_name.lower(): t.name.lower() for t in tree.find_all(exp.Table)}
        columns |= _column_names(tree, aliases)
        for group in tree.find_all(exp.Group):
            grouped |= _column_names(group, aliases)
        for where in list(tree.find_all(exp.Where)) + list(tree.find_all(exp.Having)):
            filtered |= _column_names(where, aliases)
        for func in tree.find_all(exp.AggFunc):
            aggregates.append(
                (
                    func.sql_name().lower(),
                    _column_names(func, aliases),
                    bool(func.find(exp.Distinct)),
                )
            )
        has_order = has_order or tree.find(exp.Order) is not None
        for limit in tree.find_all(exp.Limit):
            value = limit.expression
            if isinstance(value, exp.Literal) and not value.is_string:
                try:
                    limits.append(int(value.this))
                except ValueError:
                    pass
    return columns, grouped, filtered, aggregates, has_order, limits


def inspect_alignment(plan, program, materials: dict[str, Any]) -> list[dict[str, str]]:
    """Find only high-confidence mismatches; results guide repair but never guess semantics."""
    if plan is None:
        return []
    objects, field_map, relations = _catalog(materials)
    columns, grouped, filtered, aggregates, has_order, limits = _tree_facts(program)
    findings = []

    def physical(ref):
        item = field_map.get(ref)
        if not item:
            return ()
        return (
            (item["table"].lower(), item["column"].lower()),
            ("", item["column"].lower()),
        )

    for ref in plan.output_refs:
        keys = physical(ref)
        if keys and not any(key in columns for key in keys):
            findings.append({
                "kind": "missing_output", "ref": ref,
                "message": f"计划输出{ref}，SQL未引用其映射字段",
            })
    if plan.metrics:
        for ref in plan.dimension_refs:
            keys = physical(ref)
            if keys and not any(key in grouped for key in keys):
                findings.append({
                    "kind": "missing_group", "ref": ref,
                    "message": f"计划按{ref}分组，SQL未发现对应分组",
                })
    function_names = {
        "count": "count", "count_distinct": "count", "sum": "sum",
        "avg": "avg", "min": "min", "max": "max",
    }
    for metric in plan.metrics:
        expected = function_names.get(metric.operation)
        if not expected:
            continue
        refs = [key for ref in metric.field_refs for key in physical(ref)]
        matches = [
            item for item in aggregates
            if item[0] == expected and (not refs or any(key in item[1] for key in refs))
        ]
        if metric.operation == "count_distinct":
            matches = [a for a in matches if a[2]]
        if not matches:
            findings.append({
                "kind": "missing_metric", "ref": metric.id,
                "message": f"SQL未落实指标{metric.label}的{metric.operation}计算",
            })
    for item in plan.filters:
        for ref in item.field_refs:
            keys = physical(ref)
            if keys and not any(key in filtered for key in keys):
                findings.append({
                    "kind": "missing_filter", "ref": ref,
                    "message": f"计划筛选{ref}，SQL筛选条件未引用其映射字段",
                })
    if plan.sort and not has_order:
        findings.append({
            "kind": "missing_sort", "ref": plan.sort[0].target_ref,
            "message": "计划要求排序，SQL未发现ORDER BY",
        })
    if plan.limit is not None and plan.limit not in limits:
        findings.append({
            "kind": "missing_limit", "ref": str(plan.limit),
            "message": f"计划要求取前{plan.limit}项，SQL未发现对应业务LIMIT",
        })
    step_tables = []
    for step in program.steps:
        tree = parse_query(step.sql, dialect=step.dialect or "postgres")
        step_tables.append({table.name.lower() for table in tree.find_all(exp.Table)})
    for ref in plan.relation_refs:
        relation = relations.get(ref, {})
        source_name = relation.get("from") or relation.get("source")
        target_name = relation.get("to") or relation.get("target")
        source = objects.get(source_name, {}) if isinstance(source_name, str) else {}
        target = objects.get(target_name, {}) if isinstance(target_name, str) else {}
        source_table, target_table = source.get("table"), target.get("table")
        if not source_table or not target_table:
            continue
        same_source = source.get("dataSourceId") == target.get("dataSourceId")
        if same_source and not any(
            {source_table.lower(), target_table.lower()} <= tables for tables in step_tables
        ):
            findings.append({
                "kind": "missing_relation", "ref": ref,
                "message": f"计划使用关系{ref}，SQL中未发现其两端对象共同参与查询",
            })
        if not same_source and not any(step.inputs for step in program.steps):
            findings.append({
                "kind": "missing_relation", "ref": ref,
                "message": f"计划使用跨源关系{ref}，查询步骤中未发现前置结果键绑定",
            })
    return findings
