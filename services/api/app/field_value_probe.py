"""Bounded value evidence for a published field, using the normal read-only executor."""

import re
from decimal import Decimal, InvalidOperation

from semora_contracts import ClarificationOption, ClarificationQuestion
from semora_contracts.sql_compat import apply_pagination
from sqlglot import exp

from .datasources import datasource_service
from .sql_dialect import dialect_for, readonly_tree


VALUE_LIMIT = 128
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


async def inspect_values(workflow, run, ontology, packet, object_name, field):
    obj = next((entry["object"] for entry in packet["objects"]
                if entry["object"]["name"] == object_name), None)
    if obj is None:
        raise ValueError("只能探查已选对象的已发布字段；如确有必要，请先补齐对象资料")
    prop = next((p for p in obj.get("properties", []) if p["name"] == field), None)
    source_id = obj.get("dataSourceId") or ontology.get("dataSourceId")
    authorization = run.authorization
    allowed = authorization.allowed_data_source_ids if authorization else None
    if not source_id or (allowed is not None and source_id not in allowed):
        raise ValueError("数据源不在当前探查范围")
    if ontology.get("scopeField") or source_id in (ontology.get("sourceScopes") or {}):
        raise ValueError("此对象有行级范围约束，字段探查不可用")

    columns = next((s["tables"][0]["columns"] for s in packet.get("schemas", [])
                    if s["object"] == object_name and len(s.get("tables", [])) == 1), [])
    column = prop.get("column", field) if prop else ""
    scanned = next((c for c in columns if c["name"] == column), None)
    if not prop or not scanned:
        raise ValueError("只能探查已发布且经过扫描的物理字段")
    sensitivity = {"", "public", "internal"}
    if (prop.get("sensitive") or str(prop.get("sensitivity") or "").lower() not in sensitivity
            or str(scanned.get("sensitivity") or "").lower() not in sensitivity):
        raise ValueError("敏感字段不支持取值探查")

    table = obj["table"]
    parts = table.split(".")
    if len(parts) > 2 or not all(IDENTIFIER.fullmatch(p) for p in [*parts, column]):
        raise ValueError("字段探查需要有效的表和列映射")
    dialect = dialect_for(source_id)
    source = datasource_service.repository.get(source_id)
    capabilities = source.scan_result.get("capabilities") or {}
    if capabilities.get("query") != "verified":
        raise ValueError("数据源尚未验证查询能力")
    table_expr = exp.Table(this=exp.to_identifier(parts[-1], quoted=True))
    if len(parts) == 2:
        table_expr.set("db", exp.to_identifier(parts[0], quoted=True))
    value = exp.column(column, quoted=True)
    query = exp.select(value).distinct().from_(table_expr).order_by(value.copy()).limit(VALUE_LIMIT + 1)
    sql = apply_pagination(query, dialect, capabilities.get("pagination_strategy") or "baseline").sql(dialect=dialect)
    readonly_tree(sql, [table], dialect)
    result = await workflow.execute(sql, {}, [], {
        "data_source_id": source_id, "execution_id": run.id, "query_timeout_seconds": 3,
        "max_result_rows": VALUE_LIMIT, "max_result_mb": 1,
    })
    values = [row[0] for row in result.rows]
    complete = not result.truncated and len(values) <= VALUE_LIMIT
    bounded = [v for v in values[:VALUE_LIMIT]
               if v is None or isinstance(v, (bool, int, float)) or isinstance(v, str) and len(v) <= 80]
    complete = complete and len(bounded) == len(values)
    return {
        "available": True, "object": object_name, "field": field, "values": bounded,
        "complete": complete,
        "message": ("已返回查询时数据库中的全部去重值（按该数据库的比较语义）。" if complete
                    else "仅返回限额内取值，不能据此断言某个值不存在。"),
    }


def numeric_grouping_question(question, report, packet):
    """Ask about a named numeric grouping when live evidence only shows raw values."""
    if report.questions or report.technical_gaps or not report.logical_plan:
        return None
    if re.search(r"逐[一项个值日月年岁笔]|单[值岁]|每\s*[\d一二三四五六七八九十百]|原有|原始值|原表.{0,8}档|"
                 r"\d+\s*(?:到|至|[-–—~～])\s*\d+\s*(?:[^\d，。；]{0,9})(?:一组|一档|分组|区间)", question):
        return None
    dimension = next((d for d in report.requirements.dimensions
                      if d in question and re.search(r"\w(?:组|段|档|区间)$", d) and 2 < len(d) <= 24), "")
    if not dimension:
        return None
    grains = report.logical_plan.grain_refs
    if len(grains) != 1:
        return None
    for evidence in packet.get("field_value_observations", []):
        if not evidence["complete"] or f'{evidence["object"]}.{evidence["field"]}' != grains[0]:
            continue
        values = [v for v in evidence["values"] if v is not None]
        numeric = []
        for value in values:
            try:
                number = Decimal(str(value).strip())
            except InvalidOperation:
                continue
            if number.is_finite():
                numeric.append(number)
        if len(numeric) < 5 or len(numeric) + 1 < len(values):
            continue
        # A proven business grouping takes precedence over the observed raw domain.
        props = next(entry["object"]["properties"] for entry in packet["objects"]
                     if entry["object"]["name"] == evidence["object"])
        prop = next(p for p in props if p["name"] == evidence["field"])
        if prop.get("enum") or prop.get("enumValues") or packet.get("knowledge"):
            continue
        options = [ClarificationOption(
            id="individual", label="按原有数值分别统计",
            value="逐值分组，保留有效的原有特殊档位",
        )]
        question_text = f"「{dimension}」按原有的每个数值分别统计，还是按区间合并统计？"
        if max(numeric) - min(numeric) >= 20 and all(n == n.to_integral_value() for n in numeric):
            options.append(ClarificationOption(
                id="range", label="每 10 个单位一组",
                value="按从零开始的十单位区间分组；保留源数据中明确标记的开放末档",
            ))
        else:
            question_text += "如果按区间，请填写每组宽度或具体边界。"
        return ClarificationQuestion(
            id="numeric_grouping",
            field="grouping",
            question=question_text,
            reason="不同的分组边界会改变每组的数量和最终排名。",
            options=options,
        )
    return None
