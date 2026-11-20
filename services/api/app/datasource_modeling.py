"""Persistent data-source onboarding and bounded relationship discovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg

from .relationship_rules import MIN_REFERENCE_COVERAGE

from .datasource_table_storage import tables_digest
from .mdl_export import column_type
from .relationship_evidence import recall_field_pairs, finalize_relationships, technically_pending

GENERATOR_VERSION = "source-ontology-v21-bidirectional-coverage80"
PROFILE_VERSION = "bidirectional-index-random-v5"
TERMINAL = {"completed", "completed_with_pending", "failed", "cancelled"}
GENERIC_NAMES = {"id", "code", "name", "type", "status", "date"}


def _now() -> datetime:
    return datetime.now(UTC)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identifier(value: str, prefix: str = "x") -> str:
    value = re.sub(r"[^A-Za-z0-9_]", "_", value or "").strip("_")
    if not value or value[0].isdigit():
        value = prefix + "_" + value
    return value[:80] + "_" + hashlib.sha256(value.encode()).hexdigest()[:8]


def _words(*values: Any) -> set[str]:
    text = " ".join(str(value or "") for value in values).lower()
    return {word for word in re.split(r"[^a-z0-9\u4e00-\u9fff]+", text) if len(word) > 1}


def _keys(table: dict[str, Any], unique_only: bool = False) -> list[list[str]]:
    if unique_only:
        from .relationship_normalization import unconditional_unique_keys
        return unconditional_unique_keys(table)
    result = [list(item.get("columns") or []) for item in table.get("primary_keys") or []]
    result += [list(item.get("columns") or []) for item in table.get("unique_constraints") or []]
    if not unique_only:
        result += [list(item.get("columns") or []) for item in table.get("indexes") or []]
    return [item for item in result if item]


def _indexed(table: dict[str, Any], columns: list[str], *, unique: bool = False) -> bool:
    wanted = [str(value).lower() for value in columns]
    if unique:
        return bool(wanted) and any({str(v).lower() for v in key} <= set(wanted) for key in _keys(table, unique_only=True))
    return any(
        [str(value).lower() for value in (key if unique else key[: len(wanted)])] == wanted
        for key in _keys(table, unique_only=unique)
    )


def sampling_key(table):
    """Use existing unconditional integer unique keys; never create an index."""
    columns = {c['name']: c for c in table.get('columns', [])}
    for key in _keys(table, unique_only=True):
        if len(key) == 1:
            column = columns.get(key[0], {})
            if re.fullmatch(r'(?:tinyint|smallint|mediumint|int|integer|bigint|int2|int4|int8|serial|bigserial)(?:\(\d+\))?(?: unsigned)?',
                            str(column.get('type') or '').strip().lower()):
                return key[0]
    return None


def _physical_table(table: dict[str, Any]) -> str:
    return ".".join(str(table.get(key)) for key in ("catalog", "schema", "name") if table.get(key))


def _ontology_type(column: dict[str, Any]) -> str:
    from .relationship_types import relationship_family
    value = column_type(column)
    if value is None and relationship_family(column) == "number":
        return "decimal"
    value = value or "varchar"
    if value.startswith("decimal"):
        return "decimal"
    if value in {"integer", "smallint", "bigint"}:
        return "integer"
    if value in {"real", "double"}:
        return "decimal"
    if value in {"date", "timestamp", "timestamptz", "time"}:
        return value
    if value == "boolean":
        return "boolean"
    return "string"


def package_objects(
    datasource_id: str, tables: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    objects = []
    mapping = {}
    source_prefix = _identifier(datasource_id, "ds")[:18]
    for table in tables:
        table_id = str(table.get("id") or _physical_table(table))
        object_name = _identifier(f"{source_prefix}_{table_id}", "object")
        columns = {
            str(column.get("name")): column
            for column in table.get("columns") or []
            if column.get("name")
        }
        properties = []
        field_map = {}
        for column_name, column in columns.items():
            property_name = _identifier(column_name, "field")
            field_map[column_name] = property_name
            properties.append(
                {
                    "name": property_name,
                    "label": column.get("description") or column.get("comment") or column_name,
                    "description": column.get("description") or column.get("comment") or "",
                    "column": column_name,
                    "type": _ontology_type(column),
                    "sensitive": False,
                    "nullable": bool(column.get("nullable", True)),
                }
            )
        primary = next(iter(table.get("primary_keys") or []), {}).get("columns") or []
        primary_property = field_map.get(primary[0], "") if len(primary) == 1 else ""
        objects.append(
            {
                "name": object_name,
                "label": table.get("description") or table.get("name") or table_id,
                "description": table.get("description") or "",
                "dataSourceId": datasource_id,
                "table": ".".join(
                    str(table.get(key)) for key in ("schema", "name") if table.get(key)
                )
                or str(table.get("name")),
                "primaryKey": primary_property,
                "properties": properties,
                "metadata": {
                    "sourceTableId": table_id,
                    "sourcePackageId": datasource_id,
                    "objectKind": table.get("kind") or "TABLE",
                },
            }
        )
        mapping[table_id] = {"object": object_name, **field_map}
    return objects, mapping


def _candidate(
    datasource_a: str,
    datasource_b: str,
    source: dict[str, Any],
    source_columns: list[str],
    target: dict[str, Any],
    target_columns: list[str],
    scope: str,
    evidence: list[dict[str, Any]],
    legacy_score: float = 0,
    grade: str = "medium",
    status: str = "unresolved",
    domain: str | None = None,
) -> dict[str, Any]:
    value = {
        "source_table": source["id"],
        "columns": source_columns,
        "target_table": target["id"],
        "target_columns": target_columns,
    }
    candidate_id = hashlib.sha256(
        _json([datasource_a, datasource_b, domain or "", value]).encode()
    ).hexdigest()[:24]
    return {
        "id": candidate_id,
        "scope": scope,
        "source_datasource_id": datasource_a,
        "target_datasource_id": datasource_b,
        "source_table_id": source["id"],
        "target_table_id": target["id"],
        "source_columns": source_columns,
        "target_columns": target_columns,
        "key_pairs": [
            {"source": a, "target": b} for a, b in zip(source_columns, target_columns, strict=True)
        ],
        "condition_template": {
            "operator": "and_equals",
            "pairs": [
                {"left": a, "right": b} for a, b in zip(source_columns, target_columns, strict=True)
            ],
        },
        "evidence": evidence,
        "evidence_grade": grade,
        "cardinality": "N:1",
        "status": status,
        "domain": domain,
    }


def model_candidate(value, cards, table_maps, domain=None):
    """Convert one model proposal; callers isolate ValueError per proposal."""
    from .query_relationships import validated_guards
    from .relationship_normalization import join_signature
    if not isinstance(value,dict):
        raise ValueError('模型关系格式无效')
    a,b=cards.get(value.get('source_table_id')),cards.get(value.get('target_table_id'))
    if not a or not b:
        raise ValueError('模型引用不存在的表')
    if len(table_maps)>1 and a['datasource_id']==b['datasource_id']:
        return None
    pairs=value.get('key_pairs')
    if not isinstance(pairs,list) or not pairs:
        raise ValueError('模型未提供字段映射')
    pairs=sorted(pairs,key=lambda p:(str(p.get('source','')),str(p.get('target','')))) if all(isinstance(p,dict) for p in pairs) else pairs
    left={c['name'] for c in a['columns']}; right={c['name'] for c in b['columns']}
    if not all(isinstance(k,dict) and k.get('source') in left and k.get('target') in right for k in pairs):
        raise ValueError('模型引用不存在的字段')
    if len({k['source'] for k in pairs})!=len(pairs) or len({k['target'] for k in pairs})!=len(pairs):
        raise ValueError('复合映射字段重复')
    source=table_maps[a['datasource_id']][a['physical_table']]
    target=table_maps[b['datasource_id']][b['physical_table']]
    proposed={'source_datasource_id':a['datasource_id'],'target_datasource_id':b['datasource_id'],
              'source_table_id':source['id'],'target_table_id':target['id']}
    guards=validated_guards(proposed,value.get('guards',[]),table_maps)
    if guards is None:
        raise ValueError('模型提出无依据的附加条件')
    key_pairs=[{'source':k['source'],'target':k['target']} for k in pairs]
    sid=a['datasource_id']; scope='cross_source' if sid!=b['datasource_id'] else 'single_source'
    candidate=_candidate(sid,b['datasource_id'],source,[k['source'] for k in key_pairs],
        target,[k['target'] for k in key_pairs],scope,
        [{'kind':'model_discovery','reason':str(value.get('reason','')),'query_role':str(value.get('role',''))},
         {'kind':'target_unique_index','confirmed':_indexed(target,[k['target'] for k in key_pairs],unique=True)}],.8,domain=domain)
    if value.get('shared_keywords'):
        candidate['evidence'].append({'kind':'shared_keywords','keywords':list(value['shared_keywords'])})
    if value.get('family_lead_from'):
        candidate['evidence'].append({'kind':'structural_family_lead','source_table_id':value['family_lead_from']})
    if value.get('profile_statistics') is not None:
        candidate['evidence'].append({'kind':'value_profile','statistics':value['profile_statistics']})
    if value.get('direction_normalized'):
        candidate['evidence'].append({'kind':'proposal_direction_normalized','basis':'complete_unique_key'})
    candidate['condition_template']['guards']=guards
    candidate['query_role']=str(value.get('role',''))
    candidate['key_pairs']=key_pairs
    candidate['id']=join_signature(candidate)[:24]
    return candidate


def convert_model_candidates(proposed,cards,table_maps,domain=None):
    valid=[]; discarded=[]
    codes={
        '模型引用不存在的表':'unknown_relation_table',
        '模型未提供字段映射':'missing_relation_key_pairs',
        '模型引用不存在的字段':'unknown_relation_field',
        '复合映射字段重复':'duplicate_relation_field',
        '模型提出无依据的附加条件':'unsupported_relation_guard',
        '模型关系格式无效':'invalid_relation_proposal',
    }
    for value in proposed:
        try:
            candidate=model_candidate(value,cards,table_maps,domain)
            if candidate is not None:
                valid.append(candidate)
        except ValueError as exc:
            discarded.append({'stage':'candidate_conversion','code':codes.get(str(exc),'invalid_model_candidate'),
                'error_message':str(exc),
                'source_table_id':value.get('source_table_id') if isinstance(value,dict) else None,
                'target_table_id':value.get('target_table_id') if isinstance(value,dict) else None,
                'proposal':value})
    return valid,discarded


def discover_candidates(
    datasource_a: str,
    tables_a: list[dict[str, Any]],
    datasource_b: str | None = None,
    tables_b: list[dict[str, Any]] | None = None,
    domain: str | None = None,
    declared_relationships: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Legacy offline comparison helper. Production discovery uses model-selected targets."""
    datasource_b = datasource_b or datasource_a
    tables_b = tables_b or tables_a
    scope = "single_source" if datasource_a == datasource_b else "cross_source"
    result: dict[str, dict[str, Any]] = {}
    # Learn boilerplate from this catalog rather than hard-coding field names.
    key_documents = []
    for table in tables_b:
        columns = {c.get("name"): c for c in table.get("columns") or []}
        for key in _keys(table, unique_only=True):
            for name in key:
                column = columns.get(name, {})
                key_documents.append(_words(column.get("description"), column.get("comment")))
    frequencies: dict[str, int] = {}
    for document in key_documents:
        for token in document:
            frequencies[token] = frequencies.get(token, 0) + 1
    boilerplate = {token for token, count in frequencies.items()
                   if count >= max(4, len(key_documents) * 0.15)}

    def semantic_words(*values):
        # The leading description names the field; trailing clauses often hold
        # storage types, migration notes and constraints rather than its meaning.
        meanings = [re.split(r"[;；\n]", str(value or ""), maxsplit=1)[0] for value in values]
        return {word for word in _words(*meanings) - boilerplate if not word.isdigit()}
    # Declared foreign keys are authoritative for the source-local package.
    if scope == "single_source":
        lookup = {table["id"]: table for table in tables_a}
        declared = list(declared_relationships or [])
        for source in tables_a:
            declared.extend({**fk, "source_table": source["id"]} for fk in source.get("foreign_keys") or [])
        for fk in declared:
            source = lookup.get(fk.get("source_table"))
            if source:
                target = lookup.get(fk.get("target_table"))
                source_columns = list(fk.get("columns") or [])
                target_columns = list(fk.get("target_columns") or [])
                if (target and source_columns and len(source_columns) == len(target_columns)
                        and len(set(source_columns)) == len(source_columns)
                        and len(set(target_columns)) == len(target_columns)
                        and set(source_columns) <= {c["name"] for c in source.get("columns", [])}
                        and set(target_columns) <= {c["name"] for c in target.get("columns", [])}):
                    item = _candidate(
                        datasource_a,
                        datasource_b,
                        source,
                        source_columns,
                        target,
                        target_columns,
                        scope,
                        [{"kind": "foreign_key", "source": "database_constraint"}],
                        1.0,
                        "foreign_key",
                        "accepted",
                        domain,
                    )
                    result[item["id"]] = item
    # Index key registry keeps discovery sparse for large schemas.
    source_columns_by_name: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    source_columns_by_token: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    source_tables_by_id = {table["id"]: table for table in tables_a}
    for source in tables_a:
        for column in source.get("columns") or []:
            name = str(column.get("name") or "").lower()
            if name:
                source_columns_by_name.setdefault(name, []).append((source, column))
            for token in semantic_words(column.get("description"), column.get("comment")) - GENERIC_NAMES:
                source_columns_by_token.setdefault(token, []).append((source, column))
    for target in tables_b:
        target_columns_map = {
            str(column.get("name")): column for column in target.get("columns") or []
        }
        for target_columns in _keys(target, unique_only=True):
            if not target_columns:
                continue
            candidate_sets = []
            for target_column in target_columns:
                stem = str(target.get("name") or "").lower().rstrip("s")
                names = {
                    target_column.lower(),
                    f"{stem}_{target_column.lower()}",
                    f"{stem}_id",
                }
                target_tokens = (
                    semantic_words(
                        target_columns_map.get(target_column, {}).get("description"),
                        target_columns_map.get(target_column, {}).get("comment"),
                    )
                    - GENERIC_NAMES
                )
                names = {name for name in names if name not in GENERIC_NAMES
                         and (len(source_columns_by_name.get(name, [])) <= 3 or target_tokens)}
                candidate_sets.append(
                    {
                        source["id"]
                        for name in names
                        for source, _ in source_columns_by_name.get(name, [])
                    }
                    | {
                        source["id"]
                        for token in target_tokens
                        for source, _ in source_columns_by_token.get(token, [])
                    }
                )
            for source_id in set.intersection(*candidate_sets) if candidate_sets else set():
                source = source_tables_by_id[source_id]
                if datasource_a == datasource_b and source_id == target["id"]:
                    continue
                source_map = {
                    str(column.get("name")).lower(): column
                    for column in source.get("columns") or []
                }
                chosen = []
                evidence = []
                semantic = 0.0
                for target_column in target_columns:
                    stem = str(target.get("name") or "").lower().rstrip("s")
                    names = (
                        [f"{stem}_id", f"{stem}_{target_column.lower()}", target_column.lower()]
                        if target_column.lower() in {"id", "pk"}
                        else [target_column.lower(), f"{stem}_{target_column.lower()}"]
                    )
                    name = next((name for name in names if name in source_map), None)
                    matched_by_name = name is not None
                    if not name:
                        target_tokens = (
                            semantic_words(
                                target_columns_map.get(target_column, {}).get("description"),
                                target_columns_map.get(target_column, {}).get("comment"),
                            )
                            - GENERIC_NAMES
                        )
                        semantic_options = []
                        for source_name, source_column in source_map.items():
                            overlap = target_tokens & (
                                semantic_words(
                                    source_column.get("description"), source_column.get("comment")
                                )
                                - GENERIC_NAMES
                            )
                            if overlap:
                                semantic_options.append((len(overlap), source_name))
                        name = max(semantic_options, default=(0, None))[1]
                    if not name:
                        chosen = []
                        break
                    chosen.append(str(source_map[name]["name"]))
                    if name == target_column.lower():
                        semantic += 0.55
                        evidence.append({"kind": "same_name", "field": name})
                    elif matched_by_name:
                        semantic += 0.72
                        evidence.append({"kind": "entity_key_name", "field": name})
                    else:
                        semantic += 0.62
                        evidence.append({"kind": "comment_field_match", "field": name})
                    source_words = (
                        semantic_words(source_map[name].get("description"), source_map[name].get("comment"))
                        - GENERIC_NAMES
                    )
                    target_words = (
                        semantic_words(
                            target_columns_map.get(target_column, {}).get("description"),
                            target_columns_map.get(target_column, {}).get("comment"),
                        )
                        - GENERIC_NAMES
                    )
                    if source_words & target_words:
                        semantic += 0.08
                        evidence.append(
                            {
                                "kind": "comment_semantics",
                                "tokens": sorted(source_words & target_words)[:5],
                            }
                        )
                if not chosen or len(set(chosen)) != len(chosen):
                    continue
                source_map_exact = {
                    str(column.get("name")): column for column in source.get("columns") or []
                }
                compatible = all(
                    column_type(source_map_exact[a])
                    and column_type(source_map_exact[a]) == column_type(target_columns_map[b])
                    for a, b in zip(chosen, target_columns, strict=True)
                )
                if not compatible:
                    continue
                if (
                    len(chosen) == 1
                    and chosen[0].lower() in GENERIC_NAMES
                    and not any(e["kind"] == "comment_semantics" for e in evidence)
                ):
                    continue
                source_indexed = _indexed(source, chosen)
                target_unique = _indexed(target, target_columns, unique=True)
                evidence += [
                    {"kind": "type_compatible"},
                    {"kind": "target_unique_constraint"},
                    {"kind": "source_index", "confirmed": source_indexed},
                    {"kind": "target_unique_index", "confirmed": target_unique},
                    {"kind": "source_code", "status": "provider_not_configured", "locations": []},
                ]
                confidence = min(
                    0.94, semantic / max(1, len(target_columns)) + (0.04 if source_indexed else 0)
                )
                item = _candidate(
                    datasource_a,
                    datasource_b,
                    source,
                    chosen,
                    target,
                    target_columns,
                    scope,
                    evidence,
                    confidence,
                    "medium" if confidence >= 0.7 else "weak",
                    "unresolved",
                    domain,
                )
                if item["id"] not in result or (result[item["id"]]["evidence_grade"] != "foreign_key" and result[item["id"]].get("confidence", 0) < confidence):
                    result[item["id"]] = item
    for source, column, target, field, explicit in recall_field_pairs(
        [{**t, "_source_id": datasource_a} for t in tables_a],
        [{**t, "_source_id": datasource_b} for t in tables_b],
    ):
        if not column_type(column) or column_type(column) != column_type(field):
            continue
        evidence = [{"kind": "explicit_field_reference" if explicit else "semantic_recall"},
                    {"kind": "type_compatible"}, {"kind": "target_unique_constraint"},
                    {"kind": "source_index", "confirmed": _indexed(source, [column["name"]])},
                    {"kind": "target_unique_index", "confirmed": _indexed(target, [field["name"]], unique=True)}]
        item = _candidate(datasource_a, datasource_b, source, [column["name"]], target,
                          [field["name"]], scope, evidence, .9 if explicit else .75,
                          domain=domain)
        previous = result.get(item["id"])
        if previous and previous["evidence_grade"] == "foreign_key":
            continue
        if previous:
            previous["evidence"].extend(evidence[:1])
        else:
            result[item["id"]] = item
    return sorted(
        converge_candidates(list(result.values())),
        key=lambda item: (item["status"] != "accepted", -item.get("confidence", 0), item["id"]),
    )


def declared_candidates(datasource_id, tables, declared=()):
    """Only database-declared constraints bypass model discovery; no heuristic pool is built."""
    lookup = {t["id"]: t for t in tables}
    constraints = list(declared)
    for table in tables:
        constraints.extend({**fk, "source_table": table["id"]} for fk in table.get("foreign_keys") or [])
    values = {}
    for fk in constraints:
        source, target = lookup.get(fk.get("source_table")), lookup.get(fk.get("target_table"))
        left, right = fk.get("columns") or [], fk.get("target_columns") or []
        if not source or not target or not left or len(left) != len(right):
            continue
        if not set(left) <= {c["name"] for c in source.get("columns", [])} or not set(right) <= {c["name"] for c in target.get("columns", [])}:
            continue
        c = _candidate(datasource_id, datasource_id, source, left, target, right, "single_source",
            [{"kind": "foreign_key", "source": "database_constraint"}], 1., "foreign_key", "accepted")
        values[c["id"]] = c
    return list(values.values())


def converge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep bounded, evidenced alternatives per source key, never arbitrary pairs."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    authoritative = []
    for item in candidates:
        if item["evidence_grade"] == "foreign_key":
            authoritative.append(item)
            continue
        kinds = {entry["kind"] for entry in item["evidence"]}
        # A physical name shared across tables is not an entity reference.
        if not kinds & {"entity_key_name", "comment_semantics", "comment_field_match", "semantic_recall", "explicit_field_reference"}:
            continue
        key = (item["source_datasource_id"], item["source_table_id"], tuple(item["source_columns"]))
        groups.setdefault(key, []).append(item)
    output = list(authoritative)
    for key, values in groups.items():
        # A declared FK does not prohibit another independently evidenced link.
        values.sort(key=lambda i: (-i.get("confidence", 0), i["id"]))
        competing = len(values) > 1 and values[0].get("confidence", 0) - values[1].get("confidence", 0) < 0.1
        # Broad collisions are discovery noise, not thousands of review tasks.
        if competing and len(values) > 3:
            values = [item for item in values if any(e["kind"] in
                      {"explicit_field_reference", "entity_key_name", "semantic_recall"}
                      for e in item["evidence"])]
        for item in values[:3]:
            item["evidence"].append({"kind": "competition", "ambiguous": competing})
            output.append(item)
    return output


def package_relations(
    candidates: list[dict[str, Any]], mapping: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    from .relation_output import published_relationship
    result = []
    for candidate in candidates:
        if candidate["status"] != "accepted":
            continue
        left = mapping.get(candidate["source_table_id"])
        right = mapping.get(candidate["target_table_id"])
        if not left or not right:
            continue
        pairs = []
        for pair in candidate["key_pairs"]:
            if pair["source"] not in left or pair["target"] not in right:
                pairs = []
                break
            pairs.append(
                f"{left['object']}.{left[pair['source']]} = {right['object']}.{right[pair['target']]}"
            )
        from sqlglot import exp
        for guard in candidate.get("condition_template", {}).get("guards", []):
            mapping_side = left if guard["side"] == "source" else right
            if guard["field"] not in mapping_side:
                pairs = []
                break
            pairs.append(exp.EQ(this=exp.column(mapping_side[guard["field"]], table=mapping_side["object"], quoted=True),
                                expression=exp.convert(guard["value"])).sql(dialect="postgres"))
        if pairs:
            result.append(
                {
                    "name": "r_" + candidate["id"],
                    "label": "自动发现的关联",
                    "from": left["object"],
                    "to": right["object"],
                    "joinType": {"1:1": "one_to_one", "1:N": "one_to_many",
                                 "N:1": "many_to_one", "N:N": "many_to_many", "unknown": "JOIN"}[candidate["cardinality"]],
                    "cardinality": candidate["cardinality"],
                    "condition": " AND ".join(pairs),
                    "metadata": {
                        "candidateId": candidate["id"],
                        "relationshipRecord": published_relationship(candidate),
                        "evidenceGrade": candidate["evidence_grade"],
                        "queryRole": next((e.get("query_role") for e in reversed(candidate["evidence"]) if e.get("kind") == "independent_review"), ""),
                    },
                }
            )
    return result


@dataclass
class ModelingRepository:
    dsn: str | None = None

    def __post_init__(self):
        self.dsn = self.dsn if self.dsn is not None else os.getenv("METADATA_DATABASE_URL")
        self.memory_jobs = {}
        self.memory_packages = {}
        self.memory_candidates = {}

    def _connect(self):
        return psycopg.connect(self.dsn, autocommit=True)

    def save_job(self, job: dict[str, Any]):
        job = {**job, "updated_at": _now()}
        self.memory_jobs[job["id"]] = job
        if self.dsn:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO semora_meta.datasource_onboarding_jobs
                    (id,datasource_id,kind,peer_datasource_id,domain,status,phase,progress,error,details,created_at,updated_at,completed_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET status=excluded.status,phase=excluded.phase,progress=excluded.progress,
                    error=excluded.error,details=excluded.details,updated_at=excluded.updated_at,completed_at=excluded.completed_at""",
                    (
                        job["id"],
                        job["datasource_id"],
                        job.get("kind", "onboarding"),
                        job.get("peer_datasource_id"),
                        job.get("domain"),
                        job["status"],
                        job["phase"],
                        job.get("progress", 0),
                        job.get("error"),
                        _json(job.get("details", {})),
                        job.get("created_at", _now()),
                        job["updated_at"],
                        job.get("completed_at"),
                    ),
                )
        return job

    def create_job(self, datasource_id: str, **values):
        return self.save_job(
            {
                "id": uuid4().hex,
                "datasource_id": datasource_id,
                "kind": values.get("kind", "onboarding"),
                "peer_datasource_id": values.get("peer_datasource_id"),
                "domain": values.get("domain"),
                "status": "queued",
                "phase": "queued",
                "progress": 0,
                "details": dict(values.get("details") or {}),
                "created_at": _now(),
            }
        )

    def get_job(self, job_id: str):
        if job_id in self.memory_jobs:
            return self.memory_jobs[job_id]
        if not self.dsn:
            raise KeyError(job_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id,datasource_id,kind,peer_datasource_id,domain,status,phase,progress,error,details,created_at,updated_at,completed_at FROM semora_meta.datasource_onboarding_jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
        if not row:
            raise KeyError(job_id)
        keys = (
            "id",
            "datasource_id",
            "kind",
            "peer_datasource_id",
            "domain",
            "status",
            "phase",
            "progress",
            "error",
            "details",
            "created_at",
            "updated_at",
            "completed_at",
        )
        value = dict(zip(keys, row, strict=True))
        self.memory_jobs[job_id] = value
        return value

    def domain_cross_jobs(self, domain: str):
        """Latest persisted analysis per unordered source pair, including zero-result jobs."""
        if self.dsn:
            with self._connect() as connection:
                rows = connection.execute('''SELECT DISTINCT ON
                    (LEAST(datasource_id,peer_datasource_id),GREATEST(datasource_id,peer_datasource_id)) id
                    FROM semora_meta.datasource_onboarding_jobs WHERE domain=%s AND kind='cross_source'
                    AND details->>'pair_unbound' IS DISTINCT FROM 'true'
                    ORDER BY LEAST(datasource_id,peer_datasource_id),GREATEST(datasource_id,peer_datasource_id),created_at DESC''', (domain,)).fetchall()
            return [self.get_job(row[0]) for row in rows]
        latest = {}
        for job in self.memory_jobs.values():
            if (job.get('domain') != domain or job.get('kind') != 'cross_source'
                    or job.get('details', {}).get('pair_unbound')):
                continue
            pair=tuple(sorted((job['datasource_id'],job['peer_datasource_id'])))
            if pair not in latest or job['created_at'] > latest[pair]['created_at']:latest[pair]=job
        return list(latest.values())

    def latest_job(self, datasource_id: str, kind="onboarding", *, relationships_only=False):
        if self.dsn:
            with self._connect() as connection:
                row = connection.execute(
                    """SELECT id FROM semora_meta.datasource_onboarding_jobs
                    WHERE datasource_id=%s AND kind=%s
                      AND (NOT %s OR (details->>'metadata_only' IS DISTINCT FROM 'true' AND phase<>'metadata_complete'))
                    ORDER BY created_at DESC LIMIT 1""",
                    (datasource_id, kind, relationships_only),
                ).fetchone()
            return self.get_job(row[0]) if row else None
        values = [
            v
            for v in self.memory_jobs.values()
            if v["datasource_id"] == datasource_id and v["kind"] == kind
            and (not relationships_only or not (v.get("details", {}).get("metadata_only") or v["phase"] == "metadata_complete"))
        ]
        return sorted(values, key=lambda value: value["created_at"])[-1] if values else None

    def interrupted_jobs(self):
        if not self.dsn:
            return [job for job in self.memory_jobs.values() if job["status"] not in TERMINAL]
        with self._connect() as connection:
            ids = connection.execute(
                "SELECT id FROM semora_meta.datasource_onboarding_jobs WHERE status<>ALL(%s)",
                (list(TERMINAL),),
            ).fetchall()
        return [self.get_job(row[0]) for row in ids]

    def retire_generated(self, source_id, retained_ids):
        return None  # Publication replaces the complete active package.

    def replace_candidates(self, candidates, source_hash, target_hash, prune_source_id=None):
        # The package contains only active relations; this table retains the human review queue.
        if prune_source_id:
            retained = {item["id"] for item in candidates}
            self.memory_candidates = {
                key: value for key, value in self.memory_candidates.items()
                if not (value.get("source_datasource_id") == prune_source_id
                        and value.get("scope") == "single_source" and not value.get("reviewed_by")
                        and value.get("status") != "disabled" and key not in retained)
            }
            if self.dsn:
                with self._connect() as connection:
                    connection.execute(
                        """DELETE FROM semora_meta.relationship_candidates
                        WHERE source_datasource_id=%s AND scope='single_source' AND COALESCE(reviewed_by,'')=''
                        AND status<>'disabled' AND id<>ALL(%s::text[])""",
                        (prune_source_id, list(retained)))
        columns = ("id", "scope", "source_datasource_id", "target_datasource_id", "source_table_id",
                   "target_table_id", "source_columns", "target_columns", "key_pairs", "condition_template",
                   "evidence", "evidence_grade", "confidence", "cardinality", "status", "source_schema_hash",
                   "target_schema_hash", "profile_version", "domain", "reviewed_by", "review_note")
        json_fields = {"source_columns", "target_columns", "key_pairs", "condition_template", "evidence"}
        for item in candidates:
            value = {**item, "confidence": item.get("confidence") if item.get("confidence") is not None else 0.0,
                     "source_schema_hash": source_hash, "target_schema_hash": target_hash,
                     "profile_version": PROFILE_VERSION}
            self.memory_candidates[item["id"]] = value
            if not self.dsn:
                continue
            row = tuple(_json(value.get(column)) if column in json_fields else value.get(column) for column in columns)
            assignments = ",".join(f"{column}=excluded.{column}" for column in columns[1:])
            with self._connect() as connection:
                connection.execute(
                    f"""INSERT INTO semora_meta.relationship_candidates ({','.join(columns)})
                    VALUES({','.join(['%s'] * len(columns))}) ON CONFLICT(id) DO UPDATE SET {assignments},updated_at=now()""",
                    row)

    def mark_candidates_stale(self, datasource_id, schema_hash):
        return None  # Never mutate active results during research.

    def list_candidates(self, source_id, scope=None, domain=None):
        values = {}
        try:
            package = self.get_package(source_id)
            values.update((c["id"], c) for c in package["payload"].get("relationshipRecords", []))
        except KeyError:
            pass
        if self.dsn:
            columns = ("id", "scope", "source_datasource_id", "target_datasource_id", "source_table_id",
                       "target_table_id", "source_columns", "target_columns", "key_pairs", "condition_template",
                       "evidence", "evidence_grade", "confidence", "cardinality", "status", "source_schema_hash",
                       "target_schema_hash", "profile_version", "domain", "reviewed_by", "review_note")
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT {','.join(columns)} FROM semora_meta.relationship_candidates WHERE source_datasource_id=%s",
                    (source_id,)).fetchall()
            values.update((row[0], dict(zip(columns, row, strict=True))) for row in rows)
        values.update((k, v) for k, v in self.memory_candidates.items() if v["source_datasource_id"] == source_id)
        return [v for v in values.values() if v["source_datasource_id"] == source_id
                and (not scope or v["scope"] == scope) and (domain is None or v.get("domain") == domain)]

    def list_cross_candidates(self, left_id, right_id, domain):
        values = self.list_candidates(left_id, "cross_source", domain) + self.list_candidates(right_id, "cross_source", domain)
        return list({item["id"]: item for item in values if {item["source_datasource_id"], item["target_datasource_id"]} == {left_id, right_id}}.values())

    def get_candidate(self, candidate_id):
        if candidate_id in self.memory_candidates:
            return self.memory_candidates[candidate_id]
        packages = list(self.memory_packages.values())
        if self.dsn:
            with self._connect() as connection:
                packages = [{"payload": row[0]} for row in connection.execute("SELECT payload FROM semora_meta.datasource_ontology_packages").fetchall()]
        for package in packages:
            for value in package["payload"].get("relationshipRecords", []):
                if value["id"] == candidate_id:
                    return value
        raise KeyError(candidate_id)

    def decide_candidate(self, candidate_id, status, reviewer="", note=""):
        value = {**self.get_candidate(candidate_id), "status": status, "reviewed_by": reviewer, "review_note": note}
        self.memory_candidates[candidate_id] = value
        return value

    def save_package(
        self,
        datasource_id: str,
        schema_hash: str,
        payload: dict[str, Any],
        status: str,
        pending: int,
        error=None,
        *, persist=True,
    ):
        from .relation_output import published_relationship
        value = {
            "datasource_id": datasource_id,
            "schema_hash": schema_hash,
            "status": status,
            "object_count": len(payload.get("objects", [])),
            "relation_count": len(payload.get("relations", [])),
            "generator_version": GENERATOR_VERSION,
            "payload": {**payload, "relationshipRecords": [published_relationship(c) for c in payload.get("relationshipRecords", []) if c["status"] == "accepted"]},
            "error": error,
            "updated_at": _now(),
        }
        self.memory_packages[datasource_id] = value
        if self.dsn and persist:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO semora_meta.datasource_ontology_packages
                    (datasource_id,schema_hash,status,object_count,relation_count,pending_count,generator_version,payload,error)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(datasource_id) DO UPDATE SET schema_hash=excluded.schema_hash,
                    status=excluded.status,object_count=excluded.object_count,relation_count=excluded.relation_count,pending_count=excluded.pending_count,
                    generator_version=excluded.generator_version,payload=excluded.payload,error=excluded.error,generated_at=now(),updated_at=now()""",
                    (
                        datasource_id,
                        schema_hash,
                        status,
                        value["object_count"],
                        value["relation_count"],
                        0,
                        GENERATOR_VERSION,
                        _json(value["payload"]),
                        error,
                    ),
                )
        return value

    def get_package(self, datasource_id: str):
        if datasource_id in self.memory_packages:
            return self.memory_packages[datasource_id]
        if not self.dsn:
            raise KeyError(datasource_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT datasource_id,schema_hash,status,object_count,relation_count,pending_count,generator_version,payload,error,generated_at,updated_at FROM semora_meta.datasource_ontology_packages WHERE datasource_id=%s",
                (datasource_id,),
            ).fetchone()
        if not row:
            raise KeyError(datasource_id)
        keys = (
            "datasource_id",
            "schema_hash",
            "status",
            "object_count",
            "relation_count",
            "pending_count",
            "generator_version",
            "payload",
            "error",
            "generated_at",
            "updated_at",
        )
        value = dict(zip(keys, row, strict=True))
        value.pop("pending_count", None)
        self.memory_packages[datasource_id] = value
        return value

    def mark_package_stale(self, datasource_id: str, fresh_schema_hash: str):
        try:
            package = self.get_package(datasource_id)
        except KeyError:
            return
        if package["schema_hash"] == fresh_schema_hash:
            return
        package["status"] = "stale"
        package["updated_at"] = _now()
        self.memory_packages[datasource_id] = package
        if self.dsn:
            with self._connect() as connection:
                connection.execute(
                    """UPDATE semora_meta.datasource_ontology_packages
                    SET status='stale',updated_at=now() WHERE datasource_id=%s""",
                    (datasource_id,),
                )

    def save_column_profile(self, candidate: dict[str, Any], profile: dict[str, Any]):
        if not self.dsn:
            return
        statistics = {key: value for key, value in profile.items() if key != "signature"}
        rows = [
            (
                candidate["source_datasource_id"],
                candidate["source_table_id"],
                candidate["source_columns"],
                False,
            )
        ]
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO semora_meta.datasource_column_profiles
                (datasource_id,table_id,columns,profile_key,indexed,unique_key,statistics,signature,signature_version)
                VALUES(%s,%s,%s::jsonb,%s,true,%s,%s::jsonb,%s::jsonb,%s)
                ON CONFLICT(datasource_id,table_id,profile_key) DO UPDATE SET
                indexed=excluded.indexed,unique_key=excluded.unique_key,statistics=excluded.statistics,
                signature=excluded.signature,signature_version=excluded.signature_version,profiled_at=now()""",
                [
                    (
                        datasource_id,
                        table_id,
                        _json(columns),
                        hashlib.sha256(_json([candidate["source_datasource_id"], candidate["target_datasource_id"], candidate["source_table_id"], candidate["target_table_id"], candidate["key_pairs"], candidate["condition_template"], candidate.get("source_schema_hash"), candidate.get("target_schema_hash"), PROFILE_VERSION]).encode()).hexdigest(),
                        unique,
                        _json(statistics),
                        _json(profile.get("signature") or []),
                        profile.get("signature_version"),
                    )
                    for datasource_id, table_id, columns, unique in rows
                ],
            )


class ModelingService:
    def __init__(
        self,
        datasource_service,
        executor_request: Callable[..., Awaitable[Any]],
        repository: ModelingRepository | None = None,
        ontology_service=None,
    ):
        self.datasources = datasource_service
        self.executor_request = executor_request
        self.repository = repository or ModelingRepository()
        self.ontology = ontology_service
        self.tasks: dict[str, asyncio.Task] = {}
        self.source_locks: dict[str, asyncio.Lock] = {}
        self.probe_lock = asyncio.Lock()

    def start(self, datasource_id: str, *, force=False, persisted_only=False, relations_only=False, fresh=False, metadata_only=False):
        current = self.repository.latest_job(datasource_id)
        if current and current["status"] not in TERMINAL:
            return current
        job = self.repository.create_job(
            datasource_id, details={"persisted_only": persisted_only, "force": force,
                                    "relations_only": relations_only, "fresh": fresh, "metadata_only":metadata_only}
        )
        self.tasks[job["id"]] = asyncio.create_task(self._run_onboarding(job, force=force))
        return job

    def start_relationship_stage(self, datasource_id, stage, job_id=None):
        """Explicit diagnostic steps never scan a source or publish a relation package."""
        if stage not in {'keywords','retrieval','organization','discovery'}:
            raise ValueError('unknown_relationship_stage')
        item = self.datasources.repository.get(datasource_id)
        if not item.scan_result.get('tables'):
            raise ValueError('尚无已扫描结构')
        current = self.repository.latest_job(datasource_id)
        if current and current['status'] not in TERMINAL:
            raise ValueError('已有分析任务正在运行')
        if job_id:
            job = self.repository.get_job(job_id)
            if job['datasource_id'] != datasource_id or not job.get('details',{}).get('stage_only'):
                raise KeyError(job_id)
            if job.get('details',{}).get('schema_hash') != tables_digest(item.scan_result['tables']):
                raise ValueError('结构已变化，请新建关键词任务')
            job['details']['recover_stage_work'] = job['status'] in {'failed','cancelled'}
            job['details']['analysis_stage'] = stage
            self._update(job,'queued',stage,0,error=None,completed_at=None)
        else:
            if stage != 'keywords':
                raise ValueError('请先执行关键词阶段，并传入该任务ID')
            job = self.repository.create_job(datasource_id,details={'stage_only':True,'analysis_stage':stage,
                'schema_hash':tables_digest(item.scan_result['tables']), 'fresh':True})
        self.tasks[job['id']] = asyncio.create_task(self._run_relationship_stage(job))
        return job

    def relationship_stage_context(self, job):
        from .relationship_inputs import Checkpoints, build_inputs, digest
        from .relationship_keywords import KeywordDiscovery
        from semora_agent.model import structured_model
        item = self.datasources.repository.get(job['datasource_id'])
        if job.get('details',{}).get('schema_hash') != tables_digest(item.scan_result.get('tables',[])):
            raise ValueError('结构已变化，请新建关键词任务')
        cards = build_inputs({item.id:{t['id']:t for t in item.scan_result['tables']}})
        checkpoints = Checkpoints()
        fingerprint = digest([GENERATOR_VERSION,item.id,job['details']['schema_hash'],'stage-only'])
        state = checkpoints.load(job['id'],fingerprint); state['job_id']=job['id']
        def save(): checkpoints.save(job['id'],state)
        async def progress(stage,done,total):
            self._update(job,'profiling',stage,int(90*done/max(total,1)),details={**job['details'],
                'stage_done':done,'stage_total':total,'model_calls':state['metrics']['calls']})
        async def tool(kind,args): return await self._relationship_agent_tool(cards,kind,args)
        engine=KeywordDiscovery(cards,structured_model,state,save,progress,repository=self.repository,tool=tool)
        from .relationship_work import job_input_hash
        previous=job_input_hash(self.repository,job)
        if previous and previous!=engine.work.input_hash:
            raise ValueError('分析规则或结构资料已变化，请新建关键词任务；原结果仍可查看')
        return engine

    def _withhold_incomplete(self,job,candidates):
        details=job.get('details',{})
        if not (details.get('technical_incomplete') or details.get('cross_failed_batches') or details.get('cross_pair_budget_exceeded')):
            return False
        self._update(job,'completed_with_pending','incomplete',100,
            details={**details,'technical_incomplete':True,'relationships_updated':False,
                     'unpublished_proposals':len(candidates)},completed_at=_now())
        return True

    async def _run_relationship_stage(self, job):
        async with self.source_locks.setdefault(job['datasource_id'],asyncio.Lock()):
            try:
                engine = self.relationship_stage_context(job)
                job['details']['analysis_input_hash']=engine.work.input_hash
                if job['details'].pop('recover_stage_work',False) or job['details'].get('resume_research'):
                    engine.work.recover()
                job['details']['checkpoint_ref']=job['id']
                stage=job['details']['analysis_stage']
                self._update(job,'profiling',stage,0)
                if stage=='keywords':
                    from .relationship_inputs import save_inputs
                    save_inputs(self.repository,engine.cards,[job['datasource_id']])
                    result=await engine.extract_keywords()
                    job['details']['keyword_count']=len(result)
                elif stage=='retrieval':
                    result=await engine.retrieve_all()
                    job['details']['retrieved_keyword_count']=len(result)
                    job['details']['keyword_count']=len(engine.work.keywords())
                    job['details']['matched_table_count']=len({v['table_id'] for r in result for v in r['items']})
                elif stage=='organization':
                    result=await engine.organize()
                    job['details']['organization']=engine.scope['organization_metrics']
                else:
                    result=await engine.discover()
                    job['details']['proposal_count']=len(result)
                job['details']['model_calls']=engine.metrics['calls']
                gaps=engine.incomplete_units()
                job['details'].update(technical_incomplete=bool(gaps),incomplete_count=len(gaps),
                    incomplete_stages=sorted({v['stage'] for v in gaps}))
                self._update(job,'completed_with_pending' if gaps else 'completed',
                    'incomplete' if gaps else stage+'_complete',100,completed_at=_now())
            except asyncio.CancelledError:
                self._update(job,'cancelled','cancelled',job.get('progress',0),completed_at=_now())
                raise
            except Exception as exc:
                self._update(job,'failed','failed',job.get('progress',0),error=str(exc)[:1000],completed_at=_now())
            finally:
                self.tasks.pop(job['id'],None)

    def resume_failed(self, datasource_id, job_id):
        job = self.repository.get_job(job_id)
        if job['datasource_id'] != datasource_id or job.get('kind', 'onboarding') not in {'onboarding','cross_source'}:
            raise KeyError(job_id)
        if job.get('details', {}).get('pair_unbound'):
            raise ValueError('该分析所属的库间关联已解除绑定，请重新添加关联')
        current = self.repository.latest_job(datasource_id)
        if current and current['status'] not in TERMINAL:
            raise ValueError('已有分析任务正在运行')
        resumable=job['status']=='failed' or (job['status']=='completed_with_pending' and (
            job.get('kind')=='cross_source' or job.get('details',{}).get('technical_incomplete')))
        if not resumable or not job.get('details', {}).get('checkpoint_ref'):
            raise ValueError('该任务没有可恢复的失败检查点')
        job.setdefault('details', {})['resume_research'] = True
        job['details']['resume_attempt']=job['details'].get('resume_attempt',0)+1
        for key in ('stage_done','stage_total','stage_failed'):
            job['details'].pop(key,None)
        self._update(job, 'queued', 'resuming', 0, error=None, completed_at=None)
        self.tasks[job_id] = asyncio.create_task(self._run_cross_source(job) if job.get('kind')=='cross_source'
            else self._run_relationship_stage(job) if job['details'].get('stage_only')
            else self._run_onboarding(job, force=bool(job['details'].get('force'))))
        return job

    async def cancel(self, datasource_id: str, job_id: str, *, kind="onboarding"):
        job = self.repository.get_job(job_id)
        if job["datasource_id"] != datasource_id or job.get("kind", "onboarding") != kind:
            raise KeyError(job_id)
        if job["status"] in TERMINAL:
            return job
        if job.get("phase") == "publishing":
            raise ValueError("关系结果正在发布，请等待发布完成")
        scanning = job.get("phase") == "scanning"
        self._update(job, "cancelling", "cancelling", job.get("progress", 0),
                     details={**job.get("details", {}), "cancel_requested": True})
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=5)
        if scanning and self.executor_request:
            try:
                async with asyncio.timeout(5):
                    await self.executor_request("POST", f"executions/{job_id}/cancel")
            except Exception:
                # Local cancellation must not depend on a stopped executor.
                pass
        if not task or task.done():
            latest = self.repository.get_job(job_id)
            if latest["status"] not in TERMINAL:
                self._update(latest, "cancelled", "cancelled", latest.get("progress", 0),
                             error=None, completed_at=_now())
            if self.tasks.get(job_id) is task:
                self.tasks.pop(job_id, None)
        return self.repository.get_job(job_id)

    def _record_requested_cancellation(self, job):
        current = self.repository.get_job(job["id"])
        # Unrequested process shutdown keeps the checkpoint resumable.
        if current.get("details", {}).get("cancel_requested"):
            self._update(job, "cancelled", "cancelled", job.get("progress", 0),
                         details=current["details"], error=None, completed_at=_now())

    def resume(self):
        active_sources = set()
        for job in self.repository.interrupted_jobs():
            if job.get('details',{}).get('stage_only'):
                # Step-by-step experiments resume only on an explicit user request.
                self._update(job,'failed','interrupted',job.get('progress',0),error='阶段任务被中断，请显式继续',completed_at=_now())
                continue
            if job.get("details", {}).get("cancel_requested"):
                self._update(job, "cancelled", "cancelled", job.get("progress", 0),
                             error=None, completed_at=_now())
                continue
            active_sources.add(job["datasource_id"])
            job.update(status="queued", phase="resuming", error=None)
            job.setdefault("details", {})["resume_research"] = True
            self.repository.save_job(job)
            if job.get("kind") == "cross_source":
                self.tasks[job["id"]] = asyncio.create_task(self._run_cross_source(job))
            elif job.get("details", {}).get("package_only"):
                self.tasks[job["id"]] = asyncio.create_task(self._run_package_rebuild(job))
            else:
                self.tasks[job["id"]] = asyncio.create_task(
                    self._run_onboarding(job, force=bool(job.get("details", {}).get("force")))
                )
        # Backfill already-scanned sources without touching the business database.
        for source in self.datasources.repository.list():
            if source.id in active_sources or not source.scan_result.get("tables"):
                continue
            try:
                package = self.repository.get_package(source.id)
                current_hash = tables_digest(source.scan_result["tables"])
                # A software upgrade must not replace a usable online-researched
                # package with a metadata-only backfill. Explicit reanalysis updates it.
                if package["schema_hash"] == current_hash and package["status"] != "stale":
                    continue
            except KeyError:
                pass
            # Explicit analysis is required for a changed or missing package.
            continue

    def _update(self, job, status, phase, progress, **extra):
        previous_status,previous_phase=job.get('status'),job.get('phase')
        # A newly requested diagnostic stage may reset its own progress; retries,
        # follow-up evidence and phase changes within an execution may not.
        reset_stage=status=='queued' and (job.get('details',{}).get('stage_only') or (
            phase=='resuming' and previous_status in TERMINAL))
        if not reset_stage:
            progress=max(job.get('progress',0),progress)
        if status not in TERMINAL and job.get('progress',0)<100:
            progress=min(progress,99)
        job.update(status=status, phase=phase, progress=progress, **extra)
        details = job.get("details", {})
        for key in ("query_table_roles", "coverage"):
            details.pop(key, None)
        if (previous_status,previous_phase)!=(status,phase):
            import logging,sys,traceback
            diagnostic={'event':'relationship.job.failed' if status=='failed' else 'relationship.job.phase',
                'job_id':job['id'],'datasource_id':job.get('datasource_id'),'peer_datasource_id':job.get('peer_datasource_id'),
                'domain':job.get('domain'),'kind':job.get('kind','onboarding'),'status':status,'phase':phase,
                'previous_phase':previous_phase,'progress':progress,
                'counts':{k:details[k] for k in ('model_calls','stage_done','stage_total','tables','accepted','research_pending') if k in details}}
            if status=='failed':
                diagnostic.update(error=job.get('error'),error_type=type(sys.exc_info()[1]).__name__ if sys.exc_info()[1] else None,
                    traceback=traceback.format_tb(sys.exc_info()[2]))
            logging.getLogger('semora.model').log(logging.ERROR if status=='failed' else logging.INFO,_json(diagnostic))
        if status in TERMINAL and details.get("checkpoint_ref"):
            from .relationship_inputs import Checkpoints
            import json, time
            store = Checkpoints()
            path = store.path(details["checkpoint_ref"])
            if path.exists():
                value = json.loads(path.read_text())
                value["terminal_at"] = time.time()
                store.save(details["checkpoint_ref"], value)
        return self.repository.save_job(job)

    async def _profile(self, item, candidate, target_item=None):
        from .relationship_enum_context import declared_enums
        source_table = next(t for t in item.scan_result["tables"] if t["id"] == candidate["source_table_id"])
        target_table = next(t for t in (target_item or item).scan_result["tables"] if t["id"] == candidate["target_table_id"])
        async with self.probe_lock:
            try:
                return await self.executor_request(
                    "POST",
                    "datasources/profile-relationship",
                    json={
                        "datasource": {
                            "datasource_id": item.id,
                            "type": item.type,
                            "secret_ref": item.secret_ref,
                        },
                        "target_datasource": {
                            "datasource_id": target_item.id,
                            "type": target_item.type,
                            "secret_ref": target_item.secret_ref,
                        }
                        if target_item
                        else None,
                        "source_table": candidate["source_table_id"],
                        "source_columns": candidate["source_columns"],
                        "target_table": candidate["target_table_id"],
                        "target_columns": candidate["target_columns"],
                        "sample_rows": 100,
                        "source_index_confirmed": _indexed(source_table, candidate["source_columns"]),
                        "target_unique_index_confirmed": _indexed(target_table, candidate["target_columns"], unique=True),
                        "allow_bounded_unindexed": True,
                        "guards": candidate.get("condition_template", {}).get("guards", []),
                        "target_context_enums": declared_enums(target_table),
                        "source_sampling_key": sampling_key(source_table),
                        "target_sampling_key": sampling_key(target_table),
                    },
                )
            except Exception as exc:
                detail = getattr(exc, 'detail', None)
                directions = detail.get('directions', {}) if isinstance(detail, dict) else {}
                from .relationship_profile import is_timeout
                if directions and all(is_timeout(p) for p in directions.values()):
                    return {'method':'bidirectional_random_membership_v4','sample_budget':100,
                        'profile_id':detail.get('profile_id'),'raw_values_retained':False,**directions}
                raise

    async def _relationship_agent_tool(self, cards, kind, args):
        from .relationship_inputs import digest, dialect_settings
        if kind == 'definition':
            from .relationship_lineage import describe_view
            c = cards[args['table_id']]
            item = self.datasources.repository.get(c['datasource_id'])
            result = await self.executor_request('POST','datasources/relation-definitions',json={
                'datasource_id':item.id,'type':item.type,'secret_ref':item.secret_ref,'table_id':c['physical_table']})
            return describe_view(result.get('definition') or '',dialect_settings(item)[0])
        from .query_relationships import validated_guards
        r = args['relation']; a,b = cards[r['source_table_id']],cards[r['target_table_id']]
        left,right = self.datasources.repository.get(a['datasource_id']), self.datasources.repository.get(b['datasource_id'])
        maps = {item.id:{t['id']:t for t in item.scan_result['tables']} for item in (left,right)}
        pairs = r['key_pairs']
        candidate = _candidate(left.id,right.id,maps[left.id][a['physical_table']],[p['source'] for p in pairs],
            maps[right.id][b['physical_table']],[p['target'] for p in pairs],
            'single_source' if left.id==right.id else 'cross_source',[])
        guards = validated_guards(candidate,r.get('guards',[]),maps)
        if guards is None:
            raise ValueError('无依据的附加条件')
        candidate['condition_template']['guards']=guards
        result = await self._profile(left,candidate,right if left.id!=right.id else None)
        # Only aggregate statistics enter model context; signatures stay internal.
        return {'statistics':{k:v for k,v in result.items() if k!='signature'},'input_hash':digest([a,b,guards])}

    async def _semantic_review(self, candidates, table_maps, job=None, persisted_only=False, discover=True):
        from semora_agent.model import structured_model
        from .relationship_keywords import KeywordDiscovery
        from .relationship_inputs import Checkpoints, build_inputs, digest, save_inputs, dialect_settings
        from .relationship_research import research, factual_evidence
        from .relationship_normalization import join_signature
        from .query_relationships import validated_guards
        from .relationship_lineage import describe_view
        import logging
        cards = build_inputs(table_maps)
        # Query repair deliberately has only selected tables. It must never run
        # full-source input reconciliation (which prunes absent tables).
        if (job or {}).get('kind') != 'query_relation_repair':
            save_inputs(self.repository, cards, table_maps)
        checkpoints = Checkpoints()
        checkpoints.cleanup()
        job_id = (job or {}).get("id", digest(cards)[:32])
        fingerprint = digest([GENERATOR_VERSION, sorted(table_maps), (job or {}).get("domain")])
        cache_id = "cache_" + fingerprint[:32]
        state = checkpoints.load(job_id, fingerprint)
        state["job_id"] = job_id
        shared = checkpoints.load(cache_id, fingerprint)
        # Content-addressed requests and dependency-addressed research are safe to reuse across jobs.
        if not (job or {}).get("details", {}).get("fresh"):
            for namespace in ("discovery", "research"):
                state[namespace] = {**shared.get(namespace, {}), **state.get(namespace, {})}
        state["metrics"] = state.get("metrics", {"calls": 0, "cache_hits": 0})
        def save():
            checkpoints.save(job_id, state)
            checkpoints.save(cache_id, {**state, "terminal_at": __import__("time").time()})
        if job:
            job.setdefault("details", {}).update(checkpoint_ref=job_id)
        if persisted_only:
            raise ValueError("关联分析需要模型与验证能力，请执行在线分析")
        async def discovery_progress(stage, done, total):
            if job and not job.get("ephemeral"):
                start,end={'extracting_keywords':(40,48),'organizing_recall_groups':(48,54),
                           'comparing':(54,64),'analyzing_large_groups':(64,67),
                           'comparing_cross_source':(55,67)}.get(stage,(50,67))
                self._update(job, "profiling", stage, start + int((end-start) * done / max(1, total)),
                    details={**job.get("details", {}), "discovery_stage": stage, "stage_done": done,
                             "stage_total": total, "stage_failed":state.get('stage_failed',0),
                             "model_calls": state["metrics"]["calls"]})
        async def agent_tool(kind,args): return await self._relationship_agent_tool(cards,kind,args)
        if discover:
            if (job or {}).get('kind') == 'cross_source':
                from .cross_source_discovery import CrossSourceDiscovery
                engine = CrossSourceDiscovery(cards, structured_model, state, save, discovery_progress,
                    repository=self.repository, tool=agent_tool,
                    left_source_id=job['datasource_id'], right_source_id=job['peer_datasource_id'])
            else:
                engine = KeywordDiscovery(cards, structured_model, state, save, discovery_progress,
                    repository=self.repository,tool=agent_tool)
            if (job or {}).get('details',{}).get('resume_research'): engine.work.recover()
            proposed = await engine.run()
        else:
            proposed = []
        if job:
            details={"discovery_covered_tables":len(cards), "unresolved_leads":len(state.get("unresolved", [])),
                     "discarded_relations":sum(len(scope.get('discarded_relations',[])) for scope in state.get('keyword_pipeline',{}).values() if isinstance(scope,dict)),
                     "discarded_groups":sum(len(scope.get('discarded_groups',[])) for scope in state.get('keyword_pipeline',{}).values() if isinstance(scope,dict)),
                     "discarded_candidates":len(state.get('discarded_candidates',[]))}
            job.setdefault("details", {}).update(details)
            if (job or {}).get('kind') == 'cross_source':
                job['details'].update(getattr(engine, 'summary', {}))
            gaps=engine.incomplete_units() if discover else []
            conclusion_gaps=list(engine.scope.get('conclusion_gaps',{}).values()) if discover else []
            unresolved_tables={t for g in conclusion_gaps for t in g.get('table_ids',[])}
            job['details'].update(unresolved_group_count=sum(bool(g.get('table_ids') or g.get('gaps')) for g in conclusion_gaps),
                unresolved_table_count=len(unresolved_tables),
                discovery_covered_tables=max(0,len(cards)-len(unresolved_tables)))
            cross_gaps=int(job['details'].get('cross_failed_batches',0))+int(bool(job['details'].get('cross_pair_budget_exceeded')))
            job['details'].update(technical_incomplete=bool(gaps or cross_gaps),
                incomplete_count=len(gaps)+cross_gaps,
                incomplete_stages=sorted({v['stage'] for v in gaps}|({'comparing_cross_source'} if cross_gaps else set())),
                discovery_complete=not bool(gaps or cross_gaps))
        by_id = {c["id"]: c for c in candidates}
        converted,discarded=convert_model_candidates(proposed,cards,table_maps,(job or {}).get('domain'))
        for c in converted:
            by_id.setdefault(c['id'],c)
        for entry in discarded:
            state.setdefault('discarded_candidates',[]).append(entry)
            state['metrics']['discarded_candidates']=state['metrics'].get('discarded_candidates',0)+1
            logging.getLogger('semora.model').info(json.dumps({'event':'relationship.candidate.discarded',
                'job_id':(job or {}).get('id'),**entry},ensure_ascii=False))
        if state.get('discarded_candidates'):
            save()
        if job:
            job.setdefault('details',{})['discarded_candidates']=len(state.get('discarded_candidates',[]))
        # Complete joins, including guards, are the unit of research, not proposal wording or direction.
        canonical = {}
        for c in sorted(by_id.values(), key=lambda c: (c["evidence_grade"] not in {"foreign_key", "manual"}, c["id"])):
            signature = join_signature(c)
            if c['evidence_grade'] not in {'foreign_key', 'manual'} and not c.get('reviewed_by'):
                c['id'] = signature[:24]
            canonical.setdefault(signature, c)
        candidates[:] = list(canonical.values())
        from .relationship_global_context import attach_context
        attach_context(candidates)
        compact_fields = {(v["datasource_id"], v["physical_table"]): v["columns"] for v in cards.values()}
        definitions = {}
        async def definition(source_id, table_id):
            key = (source_id, table_id)
            if key not in definitions:
                item = self.datasources.repository.get(source_id)
                state["metrics"]["definition_requests"] = state["metrics"].get("definition_requests", 0) + 1
                result = await self.executor_request("POST", "datasources/relation-definitions", json={
                    "datasource_id": item.id, "type": item.type, "secret_ref": item.secret_ref, "table_id": table_id})
                definitions[key] = describe_view(result.get("definition") or "", dialect_settings(item)[0])
            return definitions[key]
        def packet_for(c):
            def card(side):
                table = table_maps[c[side + "_datasource_id"]][c[side + "_table_id"]]
                fields = set(c[side + "_columns"]) | {g["field"] for g in c.get("condition_template", {}).get("guards", []) if g["side"] == side}
                return {"table_id": table["id"], "name": table.get("name"),
                        "datasource_id": c[side + "_datasource_id"], "description": table.get("description"), "kind": table.get("kind"),
                        "fields": [v for v in table["columns"] if v["name"] in fields],
                        "context_fields": compact_fields[(c[side + "_datasource_id"], table["id"])],
                        "primary_keys": table.get("primary_keys", []), "unique_constraints": table.get("unique_constraints", [])}
            return {"candidate_id": c["id"], "source": card("source"), "target": card("target"), "key_pairs": c["key_pairs"],
                    "proposed_predicates": c.get("condition_template", {}).get("guards", []),
                    "other_proposals": c.get("other_proposals", []),
                    "target_domain_observations": c.get('target_domain_observations', []),
                    "global_review_reasons": c.get("global_review_reasons", []),
                    "evidence": factual_evidence(c["evidence"])}
        async def fetch(c, kind):
            if kind == "definition":
                views = []
                for side in ("source", "target"):
                    sid, tid = c[side + "_datasource_id"], c[side + "_table_id"]
                    if str(table_maps[sid][tid].get("kind", "")).upper() == "VIEW":
                        views.append({"side": side, "table_id": tid, **await definition(sid, tid)})
                return {"kind": "view_definition", "views": views}
            source = self.datasources.repository.get(c["source_datasource_id"])
            target = self.datasources.repository.get(c["target_datasource_id"])
            state["metrics"]["profile_requests"] = state["metrics"].get("profile_requests", 0) + 1
            profile = await self._profile(source, c, target if target.id != source.id else None)
            if not _indexed(table_maps[c['target_datasource_id']][c['target_table_id']], c['target_columns'], unique=True):
                try:
                    if await verify_target_key(c):
                        c['evidence'].append({'kind': 'verified_target_uniqueness', 'confirmed': True,
                            'method': 'bounded_full_key_check', 'scope': 'current_data',
                            'guards': c.get('condition_template', {}).get('guards', [])})
                        profile['target_uniqueness'] = 'observed_current_data'
                except Exception as exc:
                    c['evidence'].append({'kind':'key_probe_unavailable','tool':'target_key','reason':type(exc).__name__})
            self.repository.save_column_profile(c, profile)
            return {"kind": "value_profile", "statistics": {k: v for k, v in profile.items() if k != "signature"}}
        observed_unique = {}
        async def verify_target_key(c):
            from .relationship_key_probe import key_probe_queries
            sid, tid = c["target_datasource_id"], c["target_table_id"]
            guards = c.get('condition_template', {}).get('guards', [])
            cache_key = (sid, tid, tuple(sorted(c["target_columns"])), digest(guards))
            if cache_key in observed_unique:
                return observed_unique[cache_key]
            item = self.datasources.repository.get(sid)
            dialect, pagination = dialect_settings(item)
            queries = key_probe_queries(tid, c['target_columns'], guards, dialect)
            counts = []
            for query in queries:
                from semora_contracts.sql_compat import apply_pagination
                query = apply_pagination(query, dialect, pagination)
                state["metrics"]["uniqueness_queries"] = state["metrics"].get("uniqueness_queries", 0) + 1
                result = await self.executor_request("POST", "query", json={"sql": query.sql(dialect=dialect),
                    "datasource": {"datasource_id": item.id, "type": item.type, "secret_ref": item.secret_ref},
                    "timeout_ms": 10000, "max_rows": 1})
                counts.append(int(result["rows"][0][0]) if result['rows'] else 0)
            observed_unique[cache_key] = counts[0] == 0 and counts[1] > 0
            return observed_unique[cache_key]

        async def proven_unique(source_id, table_id, columns, seen=None):
            seen = set(seen or ())
            if table_id in seen:
                return False
            seen.add(table_id)
            table = table_maps[source_id][table_id]
            if _indexed(table, columns, unique=True):
                return True
            if str(table.get("kind", "")).upper() != "VIEW":
                return False
            info = await definition(source_id, table_id)
            deps = info.get("dependencies", [])
            mapped = info.get("direct_columns", {})
            if not info.get("simple_projection") or len(deps) != 1 or not all(k in mapped for k in columns):
                return False
            dep = deps[0]
            if dep not in table_maps[source_id]:
                matches = [key for key in table_maps[source_id] if key.split(".")[-1] == dep.split(".")[-1]]
                if len(matches) != 1:
                    return False
                dep = matches[0]
            return await proven_unique(source_id, dep, [mapped[k] for k in columns], seen)
        for c in list(candidates):
            from .relationship_context import field_references
            source = table_maps[c["source_datasource_id"]][c["source_table_id"]]
            target = table_maps[c["target_datasource_id"]][c["target_table_id"]]
            source_columns = {v["name"]: v for v in source["columns"]}
            if any(k["source"] not in source_columns for k in c["key_pairs"]):
                c['status']='rejected'
                c['evidence'].append({'kind':'schema_reference_invalid','code':'unknown_relation_field',
                                      'reason':'旧关联引用已删除字段'})
                state.setdefault('discarded_candidates',[]).append({'stage':'evidence_preparation',
                    'code':'unknown_relation_field','candidate_id':c['id'],'error_message':'旧关联引用已删除字段'})
                candidates.remove(c)
                continue
            if all((str(target.get("name", "")).lower(), k["target"].lower()) in set(field_references(
                source_columns[k["source"]].get("description") or source_columns[k["source"]].get("comment") or "")) for k in c["key_pairs"]):
                c["evidence"].append({"kind": "explicit_field_reference"})
            c["evidence"].append({"kind": "raw_schema_context",
                "source_description": " ".join(str(source_columns[k["source"]].get("description") or source_columns[k["source"]].get("comment") or source.get("description") or "") for k in c["key_pairs"]),
                "target_description": " ".join(str(v.get("description") or v.get("comment") or target.get("description") or "") for v in target["columns"] if v["name"] in c["target_columns"])})
            try:
                if await proven_unique(c["target_datasource_id"], c["target_table_id"], c["target_columns"]):
                    c["evidence"].append({"kind": "target_unique_index", "confirmed": True})
                elif str(target.get('kind', '')).upper() == 'VIEW' and await verify_target_key(c):
                    c["evidence"].append({"kind": "verified_target_uniqueness", "confirmed": True,
                        "method": "bounded_full_key_check", "scope": "current_data"})
            except Exception as exc:
                if isinstance(exc,ValueError) and str(exc).startswith('Unknown source dialect'):
                    raise
                c['evidence'].append({'kind':'key_probe_unavailable','tool':'target_key',
                                      'reason':type(exc).__name__,'error_message':str(exc)[:500]})
            snapshots = sorted([(c['source_datasource_id'], c['source_table_id'], source),
                                (c['target_datasource_id'], c['target_table_id'], target)], key=lambda v: (v[0], v[1]))
            fingerprint = digest([join_signature(c), snapshots,
                                  sorted(p['id'] for p in c.get('other_proposals', [])), GENERATOR_VERSION])
            cached = state["research"].get(fingerprint)
            force = (job or {}).get("details", {}).get("force") and not (job or {}).get("details", {}).get("resume_research")
            if cached and not force and cached.get("global_review_reasons", []) == c.get("global_review_reasons", []) and any(e.get("kind") == "research_complete" for e in cached.get("evidence", [])):
                c.update(cached)
            c['evidence'] = [e for e in c['evidence'] if e.get('kind') != 'target_key_context']
            c['evidence'].append({'kind': 'target_key_context', 'primary_key': any(
                set(k.get('columns', [])) == set(c['target_columns']) for k in target.get('primary_keys', []))})
            c["research_key"] = fingerprint
        if job:
            job.setdefault('details',{})['discarded_candidates']=len(state.get('discarded_candidates',[]))
        async def persist(entries):
            for c in entries:
                decisions = [next((e for e in reversed(c["evidence"]) if e.get("kind") == kind), {}) for kind in ("model_semantic_review", "independent_review")]
                optional = next((e for e in reversed(c["evidence"]) if e.get("kind") == "review_policy"), {}).get("required") is False
                if decisions[0] and optional and not decisions[1]:
                    decisions = [decisions[0]]
                if all(decisions):
                    guards = [validated_guards(c, d.get("additional_predicates", []), table_maps) for d in decisions]
                    valid = guards[0] is not None and all(g == guards[0] for g in guards)
                    original = c["condition_template"].get("guards", [])
                    if original and guards[0] != original:
                        valid = False
                    if valid:
                        c["condition_template"]["guards"] = guards[0]
                    c["evidence"] = [e for e in c["evidence"] if e.get("kind") != "join_contract"]
                    # A polymorphic reference may never lose its discriminator.
                    if any(d.get("additional_predicates") for d in decisions) and not guards[0]:
                        valid = False
                    c["evidence"].append({"kind": "join_contract", "valid": valid, "guards": guards[0] if valid else []})
                    if valid and guards[0] and all(d.get("query_outcome") == "usable" for d in decisions):
                        profile = next((e.get("statistics", {}) for e in reversed(c["evidence"]) if e.get("kind") == "value_profile"), {})
                        if profile.get("guards_applied") != guards[0]:
                            try:
                                c["evidence"].append(await fetch(c, "profile"))
                            except Exception as exc:
                                c['evidence'].append({'kind':'tool_unavailable','tool':'profile',
                                    'reason':type(exc).__name__,'error_message':str(exc)[:500]})
                target_unique = any(e.get("kind") in {"target_unique_index", "verified_target_uniqueness"} and e.get("confirmed") for e in c["evidence"])
                if all(decisions) and all(d.get("query_outcome") == "usable" for d in decisions) and not target_unique:
                    try:
                        if await verify_target_key(c):
                            c["evidence"].append({"kind": "verified_target_uniqueness", "confirmed": True,
                                "method": "bounded_full_key_check", "scope": "current_data"})
                    except Exception as exc:
                        if isinstance(exc,ValueError) and str(exc).startswith('Unknown source dialect'):
                            raise
                        c['evidence'].append({'kind':'key_probe_unavailable','tool':'target_key',
                            'reason':type(exc).__name__,'error_message':str(exc)[:500]})
                source = table_maps[c["source_datasource_id"]][c["source_table_id"]]
                su = _indexed(source, c["source_columns"], unique=True)
                tu = any(e.get("kind") in {"target_unique_index", "verified_target_uniqueness"} and e.get("confirmed") for e in c["evidence"])
                # Without proven uniqueness, plan for multiple matches rather
                # than claiming an exact single match or blocking the relation.
                c["cardinality"] = ("1:1" if su else "N:1") if tu else ("1:N" if su else "N:N")
                c['evidence'] = [e for e in c['evidence'] if e.get('kind') != 'cardinality_basis']
                c['evidence'].append({'kind':'cardinality_basis','source_unique_confirmed':su,
                    'target_unique_confirmed':tu,'method':'declared_or_verified_keys_else_conservative_many'})
                finalize_relationships([c], cross_source=c["scope"] == "cross_source")
                latest_profile = next((e.get('statistics', {}) for e in reversed(c['evidence']) if e.get('kind') == 'value_profile'), {})
                c['evidence'] = [e for e in c['evidence'] if not (e.get('kind') == 'tool_unavailable' and e.get('tool') == 'target_context')]
                if c['status'] == 'unresolved' and latest_profile.get('target_context_errors'):
                    c['evidence'].append({'kind': 'tool_unavailable', 'tool': 'target_context', 'reason': 'context_statistics_unavailable'})
                c["dependency_hashes"] = {sid + ":" + tid: digest(table_maps[sid][tid]) for sid, tid in (
                    (c["source_datasource_id"], c["source_table_id"]), (c["target_datasource_id"], c["target_table_id"]))}
                if any(e.get("kind") == "research_complete" for e in c["evidence"]) and not any(e.get("kind") in {"research_gap", "tool_unavailable", "profile_unavailable"} for e in c["evidence"]):
                    import copy
                    state["research"][c["research_key"]] = copy.deepcopy(c)
                    state.setdefault("research_incomplete", {}).pop(c["research_key"], None)
                elif any(e.get("kind") in {"research_gap", "tool_unavailable", "profile_unavailable"} for e in c["evidence"]):
                    state['research'].pop(c['research_key'], None)
                    state.setdefault("research_incomplete", {})[c["research_key"]] = {
                        "candidate_id": c["id"], "source_table": c["source_table_id"], "target_table": c["target_table_id"],
                        "issues": [e for e in c["evidence"] if e.get("kind") in {"research_gap", "tool_unavailable", "profile_unavailable"}]}
            save()
        async def progress(done, total):
            if job and not job.get("ephemeral"):
                self._update(job, "profiling", "researching", 68 + int(30 * done / max(1, total)), details={**job.get("details", {}), "researched": done, "research_total": total, "model_calls": state["metrics"]["calls"] + state["metrics"].get("research_calls", 0)})
        class ResearchModel:
            async def complete_json(_, system, user):
                if state["metrics"]["calls"] + state["metrics"].get("research_calls", 0) >= (job or {}).get('details', {}).get('model_call_limit', int(os.getenv("RELATIONSHIP_MAX_MODEL_CALLS", "2000"))):
                    raise ValueError("model_call_budget_exhausted")
                state["metrics"]["research_calls"] = state["metrics"].get("research_calls", 0) + 1
                if job:
                    job.setdefault('details', {})['model_calls'] = state['metrics']['calls'] + state['metrics']['research_calls']
                save()
                return await structured_model.complete_json(system, user)
        await research(candidates, packet_for, fetch, ResearchModel(), persist, progress)
        from .relationship_research_loop import audit_structural_acceptances
        evidence_rechecks = await audit_structural_acceptances(candidates, fetch, persist)
        if evidence_rechecks:
            state['metrics']['post_sampling_rechecks'] = len(evidence_rechecks)
            async def evidence_progress(done, total):
                if job and not job.get('ephemeral'):
                    self._update(job, 'profiling', 'auditing_evidence', 98, details={**job.get('details', {}),
                        'evidence_researched': done, 'evidence_total': total})
            await research(evidence_rechecks, packet_for, fetch, ResearchModel(), persist, evidence_progress)
        # Empty or wholly unmatched sources cannot establish an identifier domain by values.
        # Ask with actual sibling-reference statistics rather than assuming same datasource == same domain.
        from .relationship_domain_audit import domain_observations
        observations = domain_observations(candidates)
        domain_review = []
        for c in candidates:
            if c['status'] != 'accepted' or c['evidence_grade'] in {'foreign_key', 'manual'} or c.get('reviewed_by'):
                continue
            profile = next((e.get('statistics', {}) for e in reversed(c['evidence']) if e.get('kind') == 'value_profile'), {})
            observed = observations.get((c['target_datasource_id'], c['target_table_id'], tuple(c['target_columns'])), [])
            from .relationship_profile import support
            if not profile or support(profile, c.get('scope') == 'cross_source')['high_coverage'] or not any(v['source_table'] != c['source_table_id'] for v in observed):
                continue
            audit_key = digest([c['research_key'], 'target-domain-v1', observed])
            cached = state['research'].get(audit_key)
            if cached and any(e.get('kind') == 'target_domain_audit_complete' for e in cached['evidence']):
                c.update(cached)
                continue
            c['target_domain_observations'] = observed
            c['research_key'] = audit_key
            c['global_review_reasons'] = sorted(set(c.get('global_review_reasons', []) + ['unmatched_reference_identifier_domain']))
            c['evidence'] = factual_evidence(c['evidence'])
            domain_review.append(c)
        if domain_review:
            state['metrics']['domain_audits'] = len(domain_review)
            async def domain_progress(done, total):
                if job and not job.get('ephemeral'):
                    self._update(job, 'profiling', 'auditing_domains', 98, details={**job.get('details', {}),
                        'domain_researched': done, 'domain_total': total})
            await research(domain_review, packet_for, fetch, ResearchModel(), persist, domain_progress)
            for c in domain_review:
                c['evidence'].append({'kind': 'target_domain_audit_complete', 'version': 'target-domain-v1'})
            await persist(domain_review)
        research_pending = []
        for c in candidates:
            if technically_pending(c):
                c['research_pending'] = True
                issues = [e for e in c['evidence'] if e.get('kind') in {'research_gap', 'tool_unavailable', 'profile_unavailable'}]
                research_pending.append({'candidate_id': c['id'], 'source_table': c['source_table_id'],
                    'target_table': c['target_table_id'], 'issues': issues})
                logging.getLogger('semora.model').info(_json({'event': 'relationship.research_pending', 'job_id': job_id,
                    'candidate_id': c['id'], 'issues': issues}))
                continue
            c['research_pending'] = False
            if c["status"] != "accepted":
                logging.getLogger("semora.model").info(_json({"event": "relationship.not_generated", "job_id": job_id, "candidate_id": c["id"],
                    "reasons": next((e.get("reasons", []) for e in reversed(c["evidence"]) if e.get("kind") == "confirmation"), [])}))
        state['research_pending'] = research_pending
        from .query_relationships import deduplicate_query_edges
        deduplicate_query_edges(candidates)
        state["metrics"]["independent_reviews"] = sum(any(e.get("kind") == "independent_review" for e in c["evidence"]) for c in candidates)
        state["metrics"]["review_skipped"] = sum(c.get("review_required") is False for c in candidates)
        state["relation_decisions"] = [{"id": c["id"], "source_table": c["source_table_id"], "target_table": c["target_table_id"],
            "key_pairs": c["key_pairs"], "status": c["status"], "review_reasons": c.get("review_reasons", []),
            "confirmation": next((e for e in reversed(c["evidence"]) if e.get("kind") == "confirmation"), {})} for c in candidates]
        save()
        if job:
            job.setdefault('details', {}).update(model_calls=state['metrics']['calls'] + state['metrics'].get('research_calls', 0),
                researched=sum(c['evidence_grade'] not in {'foreign_key', 'manual'} for c in candidates),
                research_total=sum(c['evidence_grade'] not in {'foreign_key', 'manual'} for c in candidates),
                research_pending=len(research_pending))

    async def _run_onboarding(self, job, force=False):
        identifier = job["datasource_id"]
        persisted_only = bool(job.get("details", {}).get("persisted_only"))
        relations_only = bool(job.get("details", {}).get("relations_only"))
        lock = self.source_locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            try:
                item = self.datasources.repository.get(identifier)
                self._update(job, "testing", "testing", 8)
                if not persisted_only and not relations_only:
                    if force or item.status != "healthy":
                        await self.datasources.test(identifier)
                    self._update(job, "probing", "probing", 18)
                    try:
                        from .capability_service import validate_cached

                        await validate_cached(
                            item, self.datasources.repository, self.executor_request, force=force
                        )
                    except Exception as exc:
                        job.setdefault("details", {})["capability_warning"] = type(exc).__name__
                else:
                    self._update(job, "probing", "using_persisted_metadata", 18)
                self._update(job, "scanning", "using_persisted_metadata" if relations_only else "scanning", 32)
                if relations_only and 'tables' not in item.scan_result:
                    raise ValueError("尚无已扫描结构，请先重新扫描")
                if persisted_only and not item.scan_result.get("tables"):
                    raise ValueError("缺少已持久化的表结构，不能离线回填单源本体包")
                if not persisted_only and not relations_only and (force or not item.scan_result.get("tables")):
                    result = await self.executor_request(
                        "POST",
                        "datasources/scan",
                        json={
                            "datasource_id": item.id,
                            "type": item.type,
                            "secret_ref": item.secret_ref,
                            "execution_id": job["id"],
                        },
                    )
                    item = self.datasources.repository.record_scan(identifier, result)
                tables = item.scan_result.get("tables") or []
                schema_hash = tables_digest(tables)
                if job.get('details',{}).get('metadata_only'):
                    self._update(job,'completed','metadata_complete',100,
                        details={**job['details'],'tables':len(tables),'schema_hash':schema_hash,'relationships_updated':False},
                        completed_at=_now())
                    return
                try:
                    old_package = self.repository.get_package(identifier)
                except KeyError:
                    old_package = None
                self._update(
                    job,
                    "discovering",
                    "discovering",
                    55,
                    details={
                        **job.get("details", {}),
                        "schema_hash": schema_hash,
                        "tables": len(tables),
                    },
                )
                candidates = declared_candidates(identifier, tables, item.scan_result.get("foreign_keys") or [])
                saved = self.repository.list_candidates(identifier, "single_source")
                from .relationship_normalization import join_signature
                disabled_ids = {prior["id"] for prior in saved if prior.get("status") == "disabled"}
                disabled_signatures = {join_signature(prior) for prior in saved if prior.get("status") == "disabled"}
                # Every previously active mapping receives a targeted review, even if not rediscovered.
                for prior in saved:
                    if prior.get("status") != "accepted":
                        continue
                    if prior["source_table_id"] not in {t["id"] for t in tables} or prior["target_table_id"] not in {t["id"] for t in tables}:
                        continue
                    lookup = {t["id"]: {c["name"] for c in t.get("columns", [])} for t in tables}
                    if not all(k["source"] in lookup[prior["source_table_id"]] and k["target"] in lookup[prior["target_table_id"]] for k in prior["key_pairs"]):
                        continue
                    if any(g.get('field') not in lookup.get(prior.get(g.get('side','')+'_table_id'),set())
                           for g in prior.get('condition_template',{}).get('guards',[])):
                        continue
                    from .relationship_research import factual_evidence
                    prior = {**prior, "evidence": [e for e in factual_evidence(prior["evidence"]) if (not force and not job.get('details',{}).get('fresh') and prior.get("source_schema_hash") == schema_hash) or e.get("kind") != "value_profile"]}
                    if prior.get("evidence_grade") == "foreign_key" and prior["id"] not in {c["id"] for c in candidates}:
                        prior["evidence_grade"] = "medium"
                        prior["evidence"] = [e for e in prior["evidence"] if e.get("kind") != "foreign_key"]
                    candidates.append(prior)
                self._update(job, "profiling", "profiling", 40)
                table_map = {table["id"]: table for table in tables}
                await self._semantic_review(candidates, {identifier: table_map}, job, persisted_only)
                if self._withhold_incomplete(job,candidates):return
                # Human-disabled joins never re-enter the active or pending result sets, even with a new ID.
                candidates[:] = [c for c in candidates if c["id"] not in disabled_ids and join_signature(c) not in disabled_signatures]
                finalize_relationships(candidates)
                from .query_relationships import deduplicate_query_edges
                deduplicate_query_edges(candidates)
                self.repository.replace_candidates(candidates, schema_hash, schema_hash, prune_source_id=identifier)
                current_candidates = [{**c, "source_schema_hash": schema_hash, "target_schema_hash": schema_hash,
                    "profile_version": PROFILE_VERSION} for c in candidates if c["status"] == "accepted"]
                self._update(job, "building", "building", 99)
                objects, mapping = package_objects(identifier, tables)
                relations = package_relations(current_candidates, mapping)
                payload = {
                    "datasourceId": identifier,
                    "schemaHash": schema_hash,
                    "objects": objects,
                    "relations": relations,
                    "relationshipRecords": current_candidates,
                    "evidenceProviders": {"catalog": "enabled", "source_code": "reserved"},
                }
                if tables_digest(self.datasources.repository.get(identifier).scan_result.get("tables") or []) != schema_hash:
                    raise ValueError("研究期间结构已变化，请重新分析")
                package_status = "ready"
                staged = {"payload": payload, "schema_hash": schema_hash, "generator_version": GENERATOR_VERSION,
                          "object_count": len(objects), "relation_count": len(relations)}
                self._update(job, "building", "publishing", 99)
                package = await self._commit_source_package(identifier,schema_hash,payload,tables)
                pending_count = sum(c.get('research_pending') is True for c in candidates)
                self._update(
                    job,
                    "completed_with_pending" if pending_count else "completed",
                    "complete_with_pending" if pending_count else "complete",
                    100,
                    details={
                        **job.get("details", {}),
                        "package_status": package_status,
                        "relationships_updated": True,
                        "objects": package["object_count"],
                        "relations": package["relation_count"],
                        "not_generated": sum(c["status"] != "accepted" for c in candidates),
                        "analyzed_relations": len(candidates),
                        "research_pending": pending_count,
                    },
                    completed_at=_now(),
                )
            except asyncio.CancelledError:
                self._record_requested_cancellation(job)
                raise
            except Exception as exc:
                self._update(
                    job,
                    "failed",
                    "failed",
                    job.get("progress", 0),
                    error=str(exc)[:1000],
                    completed_at=_now(),
                )
            finally:
                self.tasks.pop(job["id"], None)

    async def _commit_source_package(self, identifier, schema_hash, payload, tables):
        from .relation_output import clean_relations
        payload = clean_relations(payload)
        if self.ontology:
            await self.ontology.update_source_package({
                'dataSourceId':identifier,'schemaHash':schema_hash,'generatorVersion':GENERATOR_VERSION,
                'payload':payload,'expectedTableHashes':{t['id']:hashlib.sha256(_json(t).encode()).hexdigest() for t in tables}})
        # Ontology Core commits the package and all affected domains in one database transaction.
        return self.repository.save_package(identifier,schema_hash,payload,'ready',0,persist=not bool(self.ontology))

    def start_cross_source(self, domain: str, left_id: str, right_id: str):
        if left_id == right_id:
            raise ValueError("请选择两个不同的数据源")
        if any(job['status'] not in TERMINAL and {job['datasource_id'],job['peer_datasource_id']}=={left_id,right_id}
               for job in self.repository.domain_cross_jobs(domain)):
            raise ValueError('这两个数据源已有分析任务正在运行')
        for identifier in (left_id, right_id):
            source = self.datasources.repository.get(identifier)
            package = self.repository.get_package(identifier)
            tables = source.scan_result.get("tables") or []
            if (
                source.status != "healthy"
                or not tables
                or package["schema_hash"] != tables_digest(tables)
                or package["status"] not in {"ready", "ready_with_pending"}
            ):
                raise ValueError("数据源尚未完成单源建模")
        job = self.repository.create_job(
            left_id, kind="cross_source", peer_datasource_id=right_id, domain=domain
        )
        self.tasks[job["id"]] = asyncio.create_task(self._run_cross_source(job))
        return job

    async def start_package_rebuild(self, datasource_id: str):
        job = self.repository.create_job(datasource_id, details={"package_only": True})
        await self._run_package_rebuild(job)
        if job["status"] == "failed":
            raise ValueError(job.get("error") or "关系发布失败")
        return job

    async def _run_package_rebuild(self, job):
        identifier = job["datasource_id"]
        lock = self.source_locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            try:
                self._update(job, "building", "building", 25)
                source = self.datasources.repository.get(identifier)
                package = self.repository.get_package(identifier)
                tables = source.scan_result.get("tables") or []
                schema_hash = tables_digest(tables)
                if package["schema_hash"] != schema_hash:
                    raise ValueError("数据结构已变化，请先完成重新扫描")
                candidates = [
                    item for item in self.repository.list_candidates(identifier, "single_source")
                    if item.get("source_schema_hash") == schema_hash
                    and item.get("target_schema_hash") == schema_hash
                    and item.get("status") != "stale"
                ]
                objects, mapping = package_objects(identifier, tables)
                relations = package_relations(candidates, mapping)
                payload = {**package["payload"], "objects": objects, "relations": relations, "relationshipRecords": [c for c in candidates if c["status"] == "accepted"]}
                staged = {**package, "payload": payload, "relation_count": len(relations)}
                self._update(job, "building", "building", 75)
                await self._commit_source_package(identifier,schema_hash,payload,tables)
                self._update(
                    job, "completed", "complete", 100,
                    details={"package_only": True, "relations": len(relations)},
                    completed_at=_now(),
                )
            except Exception as exc:
                self._update(
                    job, "failed", "failed", job.get("progress", 0),
                    error=str(exc)[:1000], completed_at=_now(),
                )
            finally:
                self.repository.memory_candidates = {k: v for k, v in self.repository.memory_candidates.items() if v["source_datasource_id"] != identifier}
                self.tasks.pop(job["id"], None)

    async def _apply_cross_relations(self, domain: str, candidates: list[dict[str, Any]], replace_pair=None):
        configuration = await self.ontology.get_configuration(domain)
        objects = {
            (
                str(obj.get("dataSourceId") or configuration.get("dataSourceId") or ""),
                str(obj.get("metadata", {}).get("sourceTableId")),
            ): obj
            for obj in configuration.get("objects", [])
        }
        relations = {relation["name"]: relation for relation in configuration.get("relations", [])}
        if replace_pair:
            object_sources = {o["name"]: str(o.get("dataSourceId") or configuration.get("dataSourceId") or "") for o in configuration.get("objects", [])}
            relations = {name: r for name, r in relations.items() if not (
                r.get("metadata", {}).get("candidateId") and {object_sources.get(r.get("from")), object_sources.get(r.get("to"))} == set(replace_pair))}
        for candidate in candidates:
            if candidate["status"] != "accepted":
                continue
            source_object = objects.get(
                (candidate["source_datasource_id"], candidate["source_table_id"])
            )
            target_object = objects.get(
                (candidate["target_datasource_id"], candidate["target_table_id"])
            )
            if not source_object or not target_object:
                continue
            source_fields = {
                prop.get("column"): prop.get("name") for prop in source_object.get("properties", [])
            }
            target_fields = {
                prop.get("column"): prop.get("name") for prop in target_object.get("properties", [])
            }
            if any(
                pair["source"] not in source_fields or pair["target"] not in target_fields
                for pair in candidate["key_pairs"]
            ):
                continue
            mapping = {"_source": {"object": source_object["name"], **source_fields},
                       "_target": {"object": target_object["name"], **target_fields}}
            generated = package_relations([{**candidate, "source_table_id": "_source", "target_table_id": "_target"}], mapping)
            if any(e.get('kind') == 'manual_domain_relation' for e in candidate.get('evidence', [])):
                from .domain_relationships import relation_for_record
                manual = relation_for_record(candidate, configuration)
                generated = [manual] if manual else []
            if generated:
                from .relation_output import published_relationship
                generated[0]["metadata"]["relationshipRecord"] = published_relationship(candidate)
            if not generated:
                candidate['status']='unresolved'
                candidate['research_pending']=True
                candidate.setdefault('evidence',[]).append({'kind':'research_gap',
                    'reason':'cross_source_mapping_incomplete','error_message':'跨源关系无法映射完整条件'})
                continue
            relations[generated[0]["name"]] = generated[0]
        configuration["relations"] = list(relations.values())
        await self.ontology.save_configuration(domain, configuration)

    async def _run_cross_source(self, job):
        try:
            left = self.datasources.repository.get(job["datasource_id"])
            right = self.datasources.repository.get(job["peer_datasource_id"])
            self.repository.get_package(left.id)
            self.repository.get_package(right.id)
            self._update(job, "discovering", "discovering", 25)
            from .relationship_normalization import join_signature
            saved = self.repository.list_cross_candidates(left.id, right.id, job['domain'])
            disabled_ids = {item['id'] for item in saved if item.get('status') == 'disabled'}
            disabled_signatures = {join_signature(item) for item in saved if item.get('status') == 'disabled'}
            candidates = [item for item in saved if item.get('status') == 'accepted']
            self._update(job, "profiling", "profiling", 55)
            lmap = {table["id"]: table for table in left.scan_result.get("tables") or []}
            rmap = {table["id"]: table for table in right.scan_result.get("tables") or []}
            input_hashes = {left.id: tables_digest(list(lmap.values())), right.id: tables_digest(list(rmap.values()))}
            await self._semantic_review(candidates, {left.id: lmap, right.id: rmap}, job)
            if any(tables_digest(self.datasources.repository.get(sid).scan_result.get("tables") or []) != fingerprint for sid, fingerprint in input_hashes.items()):
                raise ValueError("跨源研究期间结构已变化，请重新分析")
            if self._withhold_incomplete(job,candidates):return
            candidates[:] = [item for item in candidates if item['id'] not in disabled_ids and join_signature(item) not in disabled_signatures]
            finalize_relationships(candidates, cross_source=True)
            from .query_relationships import deduplicate_query_edges
            deduplicate_query_edges(candidates)
            if self.repository.get_job(job['id']).get('details',{}).get('cancel_requested'):
                raise asyncio.CancelledError()
            # Publication is one final, non-cancellable commit boundary.
            self._update(job,'building','publishing',99)
            for candidate in candidates:
                self.repository.replace_candidates([candidate],
                    input_hashes[candidate['source_datasource_id']], input_hashes[candidate['target_datasource_id']])
            if self.ontology:
                await self._apply_cross_relations(job["domain"], candidates, (left.id, right.id))
            discovery_pending = int(job.get('details', {}).get('cross_failed_batches', 0)) + int(bool(job.get('details', {}).get('cross_pair_budget_exceeded')))
            pending_count = sum(c.get('research_pending') is True for c in candidates) + discovery_pending
            self._update(
                job,
                "completed_with_pending" if pending_count else "completed",
                "complete_with_pending" if pending_count else "complete",
                100,
                details={
                    **job.get('details',{}),
                    "relationships_updated": True,
                    "candidates": len(candidates),
                    "accepted": sum(item["status"] == "accepted" for item in candidates),
                    "not_generated": sum(c["status"] != "accepted" for c in candidates),
                    "research_pending": pending_count,
                },
                completed_at=_now(),
            )
        except asyncio.CancelledError:
            self._record_requested_cancellation(job)
            raise
        except Exception as exc:
            self._update(
                job,
                "failed",
                "failed",
                job.get("progress", 0),
                error=str(exc)[:1000],
                completed_at=_now(),
            )
        finally:
            self.tasks.pop(job["id"], None)
