"""Small, complete schema packets for relationship discovery, not query execution."""
from __future__ import annotations

import json
import asyncio
import weakref
import os
import math
import re
import logging
import time
import hashlib
from uuid import uuid4

REQUEST_LIMIT = 64000
_MODEL_LIMITERS = weakref.WeakKeyDictionary()


def model_limiter():
    loop = asyncio.get_running_loop()
    if loop not in _MODEL_LIMITERS:
        _MODEL_LIMITERS[loop] = asyncio.Semaphore(max(1, min(3, int(os.getenv("RELATIONSHIP_MODEL_CONCURRENCY", "3")))))
    return _MODEL_LIMITERS[loop]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def request_size(system, payload):
    # Include the chat envelope and reserve provider/transport options.
    return len(encoded({"messages": [{"role": "system", "content": system},
                                     {"role": "user", "content": encoded(payload)}],
                        "response_format": {"type": "json_object"}, "temperature": .3}).encode()) + 1024


async def complete(model, system, payload, *, trace_context=None):
    estimated = request_size(system, payload)
    if estimated > REQUEST_LIMIT:
        raise ValueError("relationship_request_exceeds_64000_bytes")
    marker = uuid4().hex[:12]
    logger = logging.getLogger("semora.model")
    metrics = {**(trace_context or {}),"event": "relationship.model.started", "request_id": marker,
               "estimated_request_bytes": estimated, "table_count": len(payload.get("tables") or payload.get('source_a',{}).get('tables',[])+payload.get('source_b',{}).get('tables',[])),
               "candidate_count": len(payload.get("candidates", []))}
    logger.info(encoded(metrics))
    started = time.monotonic()
    try:
        from semora_agent.model_diagnostics import delivery_budget, json_model_options
        seconds = float(os.getenv("RELATIONSHIP_NO_DELIVERY_SECONDS", "300"))
        if seconds <= 0:
            raise ValueError("RELATIONSHIP_NO_DELIVERY_SECONDS must be positive")
        async with model_limiter():
            with delivery_budget(seconds, f"关联分析模型在{seconds:g}秒内未交付结果，本次任务已停止；已完成批次和检查点已保留，原生效关系不变。"):
                with json_model_options(os.getenv("RELATIONSHIP_RESPONSE_FORMAT", "json_object"), os.getenv("RELATIONSHIP_THINKING_MODE", "inherit")):
                    result = await model.complete_json(system, encoded(payload))
    except Exception as exc:
        logger.info(encoded({**metrics, "event": "relationship.model.failed", "error_type": type(exc).__name__}))
        raise
    logger.info(encoded({**metrics, "event": "relationship.model.completed", "duration_ms": round(1000 * (time.monotonic() - started))}))
    return result


def field_references(text):
    """ASCII identifiers can directly follow Chinese prose; match full paths."""
    for path in re.findall(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)(?![A-Za-z0-9_])", text or ""):
        parts = path.lower().split(".")
        yield parts[-2], parts[-1]


def literal_present(value, text):
    return bool(re.search(r"(?<![A-Za-z0-9_.])" + re.escape(str(value)) + r"(?![A-Za-z0-9_.])", text))


def protected_fields(tables):
    protected = {t["id"]: set() for t in tables}
    names = {str(t.get("name", "")).lower(): t for t in tables}
    for t in tables:
        for key in (t.get("primary_keys") or []) + (t.get("unique_constraints") or []) + [i for i in t.get("indexes", []) if i.get("unique")]:
            protected[t["id"]].update(key.get("columns") or [])
        for fk in t.get("foreign_keys") or []:
            protected[t["id"]].update(fk.get("columns") or fk.get("source_columns") or [])
            target_name = str(fk.get("referenced_table") or fk.get("target_table") or "").lower()
            target = names.get(target_name.split(".")[-1])
            if target:
                protected[target["id"]].update(fk.get("referenced_columns") or fk.get("target_columns") or [])
        for c in t.get("columns") or []:
            if c.get("primary_key"):
                protected[t["id"]].add(c["name"])
            for table, field in field_references(c.get("description") or c.get("comment") or ""):
                target = names.get(table)
                match = next((v for v in (target or {}).get("columns", []) if v["name"].lower() == field), None)
                if match:
                    protected[t["id"]].add(c["name"])
                    protected[target["id"]].add(match["name"])
    return protected


def relationship_columns(table, protected=(), profile=None):
    """Omit measurements, not authoritative identity/reference/guard fields."""
    result = []
    field_roles = (profile or {}).get("field_roles", {})
    for c in table.get("columns") or []:
        name = c["name"]
        description = str(c.get("description") or c.get("comment") or "")
        role = field_roles.get(name)
        if (name in protected or c.get("primary_key")
                or role in {"identifier", "reference", "polymorphic_reference", "discriminator"}
                or re.search(r"(?:id|code|key|serialno|number|no|type|kind|category|typelist)$", name, re.I)
                or re.search(r"编号|标识|代码|流水号|序号|主键|外键|对象类型|实体类型", description)):
            result.append(c)
            continue
        kind = str(c.get("type") or c.get("raw_type") or "").lower().strip()
        if str(table.get("kind", "")).upper() == "VIEW" and kind in {"text", "clob", "nclob"}:
            origin = re.search(r"原类型\s*=\s*([A-Za-z]+\s*(?:\([^)]*\))?)", description)
            # Widened view strings are not proof of long-form text.
            kind = origin.group(1).lower() if origin else "unknown"
        omitted = bool(re.match(r"(?:date|datetime|timestamp|time|interval|float|double|real|money|smallmoney|tinytext|text|mediumtext|longtext|clob|nclob|blob|bytea|binary|varbinary|image|json|jsonb|xml)\b", kind))
        if (re.search(r"(?:date|time|timestamp|_at)$", name, re.I)
                or re.search(r"日期|时间戳|时间|月份", re.split(r"[;；\n]", description)[0])):
            omitted = True
        numeric = re.match(r"(?:decimal|numeric|number)\s*\(\s*\d+\s*,\s*(\d+)\s*\)", kind)
        if numeric and int(numeric.group(1)) > 0:
            omitted = True
        length = re.match(r"(?:n?varchar|n?char|nvarchar2|varchar2)\s*\(\s*(\d+)\s*\)", kind)
        if length and int(length.group(1)) >= 1000:
            omitted = True
        if not omitted and role not in {"time", "measure"}:
            result.append(c)
    return result


def table_card(table, columns):
    return {"table_id": table["id"], "name": table.get("name"), "kind": table.get("kind"),
            "description": table.get("description"), "primary_keys": table.get("primary_keys", []),
            "unique_constraints": table.get("unique_constraints", []),
            "foreign_keys": table.get("foreign_keys", []),
            "columns": [{"name": c["name"], "type": c.get("type"),
                         "description": c.get("description") or c.get("comment")} for c in columns]}


def table_batches(cards, system):
    """At most twenty detailed tables, additionally constrained by request bytes."""
    n = len(cards)
    count = max(1, math.ceil(n / 20), n // 20)
    width, remainder = divmod(n, count)
    offset = 0
    for i in range(count):
        amount = width + (i < remainder)
        group = cards[offset:offset + amount]
        offset += len(group)
        batch = []
        for card in group:
            if request_size(system, {"tables": batch + [card]}) > REQUEST_LIMIT:
                if batch:
                    yield batch, True
                    batch = []
                if request_size(system, {"tables": [card]}) > REQUEST_LIMIT:
                    # Split only a table which cannot fit on its own. No silent truncation.
                    fragment = []
                    for column in card["columns"]:
                        if fragment and request_size(system, {"tables": [{**card, "columns": fragment + [column], "partial_columns": True}]}) > REQUEST_LIMIT:
                            yield [{**card, "columns": fragment, "partial_columns": True}], True
                            fragment = []
                        fragment.append(column)
                    part = {**card, "columns": fragment, "partial_columns": True}
                    yield [part], request_size(system, {"tables": [part]}) <= REQUEST_LIMIT
                    continue
            batch.append(card)
        if batch:
            yield batch, True


def relation_payload(packets, required_ids=(), **extra):
    """Share each table card once; retain per-candidate field mappings and evidence."""
    tables, candidates, proposal_catalog = {}, [], {}
    target_domains = {}
    def ref(card):
        if not isinstance(card, dict) or "table_id" not in card:
            return card
        key = card["table_id"]
        if "datasource_id" in card:
            key = card["datasource_id"] + ":" + key
        if key not in tables:
            tables[key] = {k: v for k, v in card.items() if k not in {"fields", "context_fields", "field_directory"}}
            tables[key]["field_roles"] = dict(card.get("field_roles", {}))
            tables[key]["fields"] = []
        known = {c["name"] for c in tables[key]["fields"]}
        for field in card.get("fields", []) + card.get("context_fields", []):
            if field["name"] not in known:
                tables[key]["fields"].append(field)
                known.add(field["name"])
        tables[key].setdefault("field_roles", {}).update(card.get("field_roles", {}))
        return {"table_ref": key, "fields": [c["name"] for c in card.get("fields", [])]}
    for packet in packets:
        candidate = {k: v for k, v in packet.items() if k not in {"source", "target", "other_targets"}}
        neighbors = candidate.pop("other_proposals", [])
        if neighbors:
            candidate["other_proposal_ids"] = [r['id'] for r in neighbors]
            for relation in neighbors:
                compact = {k: v for k, v in relation.items() if k not in {'source_columns', 'target_columns', 'condition_template'}}
                if relation.get('condition_template', {}).get('guards'):
                    compact['guards'] = relation['condition_template']['guards']
                proposal_catalog[relation['id']] = compact
        observations = candidate.pop('target_domain_observations', [])
        if observations:
            key = hashlib.sha256(encoded(observations).encode()).hexdigest()[:20]
            target_domains[key] = observations
            candidate['target_domain_ref'] = key
        for side in ("source", "target"):
            if side in packet:
                candidate[side] = ref(packet[side])
        if "other_targets" in packet:
            candidate["other_targets"] = [{**item, "target": ref(item["target"])} for item in packet["other_targets"]]
        candidates.append(candidate)
    # required_ids remains a compatibility argument for callers; candidate_id is the only wire ID.
    extra.pop('targeted_recheck',None)
    extra.pop('required_decision_ids',None)
    result={"tables":tables,"candidates":candidates,**extra}
    if proposal_catalog:
        result['other_proposals']=proposal_catalog
    if target_domains:
        result['target_domains']=target_domains
    return result


def relation_batches(packets, system):
    batch = []
    for packet in packets:
        trial = relation_payload(batch + [packet], [p["candidate_id"] for p in batch + [packet]])
        current_count = len(relation_payload(batch)["tables"]) if batch else 0
        new_count = len(trial["tables"])
        if batch and (len(batch) >= 20 or new_count > 40 or request_size(system, trial) > REQUEST_LIMIT - 512):
            yield batch, True
            batch = []
            trial = relation_payload([packet], [packet["candidate_id"]])
        if request_size(system, trial) > REQUEST_LIMIT - 512:
            yield [packet], False
        else:
            batch.append(packet)
    if batch:
        yield batch, True
