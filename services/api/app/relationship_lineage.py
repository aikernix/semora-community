"""Verified view provenance is evidence, never a business relationship itself."""
from __future__ import annotations

import sqlglot
from sqlglot import exp


def describe_view(definition, dialect):
    try:
        tree = sqlglot.parse_one(definition, read=dialect)
    except Exception:
        return {"status": "unavailable", "reason": "definition_parse_failed"}
    tables = list(tree.find_all(exp.Table))
    dependencies = [".".join(v for v in (t.catalog, t.db, t.name) if v) for t in tables]
    simple = (isinstance(tree, exp.Select) and len(tables) == 1
              and not any(tree.find(k) for k in (exp.Join, exp.Where, exp.Group, exp.Having,
                                                exp.Limit, exp.Distinct, exp.AggFunc, exp.Window)))
    mappings = {}
    if simple:
        for expression in tree.expressions:
            column = expression.this if isinstance(expression, exp.Alias) else expression
            if isinstance(column, exp.Column):
                mappings[expression.alias_or_name] = column.name
    return {"status": "available", "dependencies": dependencies,
            "simple_projection": simple, "direct_columns": mappings}


def direct_lineage(candidate, definitions):
    """Only identical, unfiltered projected keys justify deterministic removal."""
    if candidate.get("condition_template", {}).get("guards"):
        return False
    for side, other in (("source", "target"), ("target", "source")):
        if candidate[side + "_datasource_id"] != candidate[other + "_datasource_id"]:
            continue
        info = definitions.get((candidate[side + "_datasource_id"], candidate[side + "_table_id"]), {})
        if (not info.get("simple_projection") or info.get("dependencies") != [candidate[other + "_table_id"]]):
            continue
        if all(info.get("direct_columns", {}).get(pair[side]) == pair[other] for pair in candidate["key_pairs"]):
            return True
    return False


def prune_shared_entity_edges(candidates, roles):
    """Prefer two proven spokes to an inferred sibling edge; preserve direct references."""
    active = [c for c in candidates if c["status"] == "accepted"]
    incoming = {}
    for c in active:
        incoming[c["target_table_id"]] = incoming.get(c["target_table_id"], 0) + 1
    def rank(table):
        role = roles.get(table, {}).get("role")
        return (role not in {"entity_master", "dimension"}, -incoming.get(table, 0), table)
    spokes = {}
    for c in active:
        if c.get("condition_template", {}).get("guards"):
            continue
        if rank(c["target_table_id"]) >= rank(c["source_table_id"]):
            continue
        spokes.setdefault(c["source_table_id"], []).append(c)
    for c in active:
        if c["evidence_grade"] in {"foreign_key", "manual"} or c.get("reviewed_by"):
            continue
        if c.get("condition_template", {}).get("guards") or any(e.get("kind") == "explicit_field_reference" for e in c["evidence"]):
            continue
        # A direct record reference is distinct from sharing an entity identifier.
        if any(e.get("relationship_kind") == "direct_record_reference" for e in c["evidence"]):
            continue
        for left in spokes.get(c["source_table_id"], []):
            for right in spokes.get(c["target_table_id"], []):
                if (left["target_table_id"] != right["target_table_id"]
                        or left["target_datasource_id"] != right["target_datasource_id"]
                        or rank(left["target_table_id"]) >= min(rank(c["source_table_id"]), rank(c["target_table_id"]))):
                    continue
                a = {k["source"]: k["target"] for k in left["key_pairs"]}
                b = {k["source"]: k["target"] for k in right["key_pairs"]}
                if (len(a) == len(b) == len(c["key_pairs"])
                        and all(k["source"] in a and k["target"] in b and a[k["source"]] == b[k["target"]] for k in c["key_pairs"])):
                    c["status"] = "rejected"
                    c["evidence"].append({"kind": "verified_shared_entity_path", "via_candidate_ids": [left["id"], right["id"]]})
                    c["evidence"].append({"kind": "automatic_exclusion", "code": "shared_entity_path",
                        "via_candidate_ids": [left["id"], right["id"]],
                        "reason": "完整字段映射已通过同一主体连接，无独立直接引用依据"})
                    break
            if c["status"] == "rejected":
                break
