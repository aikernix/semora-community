"""Version-bound retrieval of relevant ontology, schema metadata and knowledge."""

import json
import re
from copy import deepcopy
from pydantic import BaseModel, ConfigDict, Field
from semora_agent.prompts import CONTEXT_SELECTION
from .agent_context import compact_definition
from .datasources import datasource_service
from .knowledge import knowledge_service
from .knowledge_retrieval import retrieve_knowledge
from .knowledge_retrieval import knowledge_excerpt as knowledge_excerpt
from .run_state import binding, digest
from .ontology_paths import connect_objects


class ObjectUse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phrase: str = Field(
        min_length=1, max_length=100, description="需求或确认答案中的原文对象/归属短语"
    )
    object: str = Field(min_length=1, max_length=100, description="承载该短语的目录对象name")


class ContextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_uses: list[ObjectUse] = Field(
        default_factory=list,
        max_length=12,
        description="先逐项映射需求中提到的对象与归属来源，避免只覆盖取数主表而漏掉所属对象。",
    )
    objects: list[str] = Field(
        default_factory=list, max_length=8, description="当前目录中完成需求必需的准确对象name。"
    )
    knowledge_keywords: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="按需求选择必要的检索词，最多5个，每项单独检索，不为凑数量扩展主题。",
    )



def field_candidates(objects, text, limit=48):
    """Small, exact-label retrieval hints; never ship the complete field catalog."""
    text = text.casefold()
    rows = []
    for obj in objects.values():
        label = obj.get("label") or obj["name"]
        overlap = sum(label[i : i + 2].casefold() in text for i in range(len(label) - 1))
        for prop in obj.get("properties", []):
            aliases = prop.get("aliases") or []
            names = [prop.get("label"), *([aliases] if isinstance(aliases, str) else aliases)]
            matches = []
            for name in names:
                if not isinstance(name, str) or not 2 <= len(name) <= 40:
                    continue
                token = name.casefold()
                matched = (
                    token in text
                    if re.search(r"[\u4e00-\u9fff]", token)
                    else re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", text)
                )
                if matched:
                    matches.append(name)
            if matches:
                matched = max(matches, key=len)
                rows.append((len(matched), overlap, obj["name"], prop["name"], matched))
    rows.sort(key=lambda r: (-r[0], -r[1], r[2], r[3]))
    return [{"object": o, "field": f, "label": label} for _, _, o, f, label in rows[:limit]]


def lightweight_object_catalog(available, relations, text, limit=50):
    """Rank a large catalog deterministically; relationship expansion uses stored graph edges."""
    folded = text.casefold()

    def tokens(value):
        value = str(value or "").casefold()
        words = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", value))
        for word in list(words):
            if re.search(r"[\u4e00-\u9fff]", word):
                words.update(word[index:index + 2] for index in range(len(word) - 1))
        return words

    query_tokens = tokens(text)
    ranked = []
    for name, obj in available.items():
        labels = [name, obj.get("label"), obj.get("description"), *(obj.get("aliases") or [])]
        labels += [prop.get("label") for prop in obj.get("properties", [])]
        object_tokens = set().union(*(tokens(value) for value in labels))
        overlap = len(query_tokens & object_tokens)
        exact = sum(3 for value in labels if value and str(value).casefold() in folded)
        ranked.append((overlap + exact, name))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    seeds = [name for score, name in ranked if score > 0][:20]
    if not seeds:
        seeds = [name for _, name in ranked[: min(20, limit)]]
    selected = list(seeds)
    selected_set = set(selected)
    for relation in relations:
        if len(selected) >= limit:
            break
        if relation.get("from") in selected_set and relation.get("to") in available:
            selected_set.add(relation["to"]); selected.append(relation["to"])
        elif relation.get("to") in selected_set and relation.get("from") in available:
            selected_set.add(relation["from"]); selected.append(relation["from"])
    for _, name in ranked:
        if len(selected) >= limit:
            break
        if name not in selected_set:
            selected_set.add(name); selected.append(name)
    return {name: available[name] for name in selected}



def input_binding(run, ontology, scope):
    facts = {f.question_id: {"field": f.field, "value": f.value} for f in run.user_answers}
    scans = {}
    for source_id in {
        o.get("dataSourceId") or ontology.get("dataSourceId")
        for o in authorized_objects(run, ontology).values()
    }:
        try:
            source = datasource_service.repository.get(source_id)
            scans[source_id] = {
                "at": source.last_scan_at,
                "status": source.status,
                "version": (source.scan_result.get("connection") or {}).get("version"),
                "capabilities": source.scan_result.get("capabilities", {}),
            }
        except KeyError:
            scans[source_id] = "not_registered"
    return digest(
        {
            "context_protocol": 11,
            "binding": binding(run, ontology, scope),
            "question": run.question,
            "answers": facts,
            "answer_context": run.agent_memory.get("confirmed_fact_context", []),
            "scans": scans,
        }
    )


def confirmed_fact_context(workflow, run):
    """Keep the meaning of a selected option, including legacy explanations."""
    previous = (run.continuation or {}).get("confirmed_fact_context", [])
    saved = run.agent_memory.get("confirmed_fact_context", [])
    definitions = []
    seen = set()
    current = run
    for _ in range(12):
        if current.id in seen:
            break
        seen.add(current.id)
        drafts = [
            current.requirement_draft.model_dump(mode="json") if current.requirement_draft else {},
            *[r.get("draft") or {} for r in reversed(current.alignment_rounds)],
            (current.continuation or {}).get("draft") or {},
        ]
        definitions.extend(q for d in drafts for q in d.get("clarifications", []))
        parent = getattr(workflow.store, "runs", {}).get(
            (current.continuation or {}).get("source_run_id")
        )
        if parent is None or parent.domain != run.domain:
            break
        current = parent
    rows = []
    for fact in run.user_answers:
        row = fact.model_dump(mode="json")
        inherited = next(
            (
                r
                for r in [*saved, *previous]
                if all(r.get(k) == row.get(k) for k in ("question_id", "field", "value"))
                and r.get("clarification_question")
            ),
            None,
        )
        if inherited:
            row.update(
                {
                    k: inherited[k]
                    for k in ("clarification_question", "selected_option_description")
                    if k in inherited
                }
            )
        else:
            for q in definitions:
                if q.get("id") != fact.question_id or q.get("field") != fact.field:
                    continue
                row["clarification_question"] = q.get("question", "")
                option = next(
                    (
                        o
                        for o in q.get("options", [])
                        if fact.value in (o.get("value"), o.get("id"))
                    ),
                    None,
                )
                if option:
                    row.update(
                        clarification_question=q.get("question", ""),
                        selected_option_description=option.get("description", ""),
                    )
                break
        rows.append(row)
    run.agent_memory["confirmed_fact_context"] = rows
    return rows


def authorized_objects(run, ontology):
    return {o["name"]: o for o in ontology.get("objects", [])}


def context_packet(workflow, run, ontology, names):
    available = authorized_objects(run, ontology)
    identity_objects = lightweight_object_catalog(
        available, ontology.get("relations", []), getattr(run, "question", ""), limit=50
    )
    selected = set(names)
    if not selected <= available.keys():
        raise ValueError("上下文请求包含未发布或未授权对象")
    relations = [
        r for r in ontology.get("relations", []) if r["from"] in available and r["to"] in available
    ]
    gaps = []
    if len(selected) > 1:
        try:
            # Already connected selected objects need no external bridge.
            # An alternate path through an unrelated object only adds noise.
            seen = {next(iter(selected))}
            while True:
                expanded = seen | {
                    n
                    for r in relations
                    if {r["from"], r["to"]} <= selected and ({r["from"], r["to"]} & seen)
                    for n in (r["from"], r["to"])
                }
                if expanded == seen:
                    break
                seen = expanded
            if seen != selected:
                paths = connect_objects(selected, relations)
                for r in relations:
                    if r["name"] in paths:
                        selected.update([r["from"], r["to"]])
        except ValueError:
            gaps.append("部分对象未配置可连通关系；不得猜测关联条件")
    if len(selected) > 12:
        raise ValueError("所需关联上下文超过12个对象，请缩小本次查询范围")
    objects = []
    schemas = []
    for name in sorted(selected):
        obj = available[name]
        objects.append({"object": compact_definition(obj)})
        source_id = obj.get("dataSourceId") or ontology.get("dataSourceId")
        try:
            source = datasource_service.repository.get(source_id)
            table = obj.get("table", "")
            def table_id(t):
                return t.get("id") or ".".join(str(t.get(k)) for k in ("catalog","schema","name") if t.get(k))
            scanned = source.scan_result.get("tables", [])
            matches = [t for t in scanned if table_id(t) == table]
            if not matches and "." not in table:
                matches = [t for t in scanned if (t.get("name") or t.get("table_name")) == table]
                if len(matches)>1:
                    gaps.append(f"对象 {name} 的物理表 {table} 跨 Schema 重名，请补全映射")
                    matches=[]
            # Only scanned structural metadata. Never include secret refs or rows.
            tables = [
                {
                    k: deepcopy(t[k])
                    for k in (
                        "name",
                        "table_name",
                        "schema",
                        "catalog",
                        "id",
                        "columns",
                        "primary_keys",
                        "unique_constraints",
                        "primary_key",
                        "foreign_keys",
                    )
                    if k in t
                }
                for t in matches
            ]
            schemas.append({"object": name, "tables": tables, "scanned": bool(tables)})
        except KeyError:
            schemas.append({"object": name, "tables": [], "scanned": False})
    metrics = []
    for metric in ontology.get("metrics", []):
        refs = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.", str(metric.get("expression", ""))))
        if metric.get("grain") in selected or refs and refs <= selected:
            metrics.append(compact_definition(metric))
    return {
        "selected_objects": sorted(selected),
        "identity_catalog": [
            {"object": o["name"], "label": o.get("label"), "primaryKey": o["primaryKey"]}
            for o in identity_objects.values()
            if o.get("primaryKey")
        ],
        "objects": objects,
        "schemas": schemas,
        "relations": [compact_definition(r) for r in relations if {r["from"], r["to"]} <= selected],
        "metrics": metrics,
        "gaps": gaps,
    }


async def prepare_context(workflow, run, question, ontology, scope):
    key = input_binding(run, ontology, scope)
    saved = (run.agent_memory.get("prepared_context") or [{}])[-1]
    if saved.get("binding") == key:
        workflow.emit(
            run,
            "context.reused",
            run.status,
            {
                "summary": "复用当前需求版本的准备资料，不重新探索",
                "objects": saved["packet"]["selected_objects"],
            },
            "context_service",
        )
        return saved
    available = authorized_objects(run, ontology)
    kb_ids = ontology.get("knowledgeBaseIds") or []
    facts = run.agent_memory.get("confirmed_fact_context") or [
        f.model_dump(mode="json") for f in run.user_answers
    ]
    catalog = lightweight_object_catalog(
        available,
        ontology.get("relations", []),
        question + " " + " ".join(str(f.get("value", "")) for f in facts),
    )
    retrieval = await retrieve_knowledge(
        workflow.model, question, kb_ids, client=knowledge_service,
        confirmed_answers=facts, evidence=run.evidence,
        discoveries=run.agent_memory.setdefault("knowledge_discovery", []),
    )
    packet = retrieval.packet
    workflow.emit(
        run,
        "knowledge.prepared",
        run.status,
        {
            "summary": "已检索本次需求的辅助定义；根据取得的资料选择相关本体",
            "knowledge_pages": len(packet["knowledge"]),
            "knowledge_bases": len(kb_ids),
        },
        "context_service",
    )
    selection = ContextSelection(objects=list(available)) if len(available) <= 1 else None
    if selection is None:
        raw = await workflow.model.complete_json(
            CONTEXT_SELECTION,
            json.dumps(
                {
                    "request": question,
                    "confirmed_answers": facts,
                    "knowledge": packet["knowledge"],
                    "object_catalog": [
                        {k: o.get(k) for k in ("name", "label", "description", "primaryKey")}
                        for o in catalog.values()
                    ],
                    "field_candidates": field_candidates(
                        catalog, question + " " + " ".join(str(f.get("value", "")) for f in facts)
                    ),
                    "relation_catalog": {
                        "columns": ["name", "from", "to"],
                        "rows": [
                            [r.get(k) for k in ("name", "from", "to")]
                            for r in ontology.get("relations", [])
                            if r.get("from") in catalog and r.get("to") in catalog
                        ],
                    },
                    "metrics": [
                        {k: m.get(k) for k in ("name", "label", "grain")}
                        for m in ontology.get("metrics", [])
                    ],
                    "output_contract": ContextSelection.model_json_schema(),
                },
                ensure_ascii=False,
            ),
        )
        selection = ContextSelection.model_validate(raw)
    selection.objects = list(
        dict.fromkeys([*selection.objects, *(use.object for use in selection.object_uses)])
    )
    selection = ContextSelection.model_validate(selection.model_dump())
    if not set(selection.objects) <= available.keys():
        raise ValueError("上下文选择包含未发布或未授权对象，已停止准备")
    packet = {**context_packet(workflow, run, ontology, selection.objects), **packet}
    saved = {"binding": key, "packet": packet, "supplements": 0}
    run.agent_memory["prepared_context"] = [saved]
    workflow.emit(
        run,
        "context.prepared",
        run.status,
        {
            "summary": "已准备相关对象、关系、扫描结构及辅助证据；进入规划，不开放自由探索",
            "objects": packet["selected_objects"],
            "relations": len(packet["relations"]),
            "knowledge_pages": len(packet["knowledge"]),
        },
        "context_service",
    )
    return saved


def supplement_context(workflow, run, ontology, state, names, purpose, reason, scope):
    if state.get("binding") != input_binding(run, ontology, scope):
        raise ValueError("准备资料的权限或版本已变化，需要重新准备")
    if not reason.strip():
        raise ValueError("必须说明缺少哪个核心信息及其对结果的影响")
    available = authorized_objects(run, ontology)
    if not names or len(names) > 4 or not set(names) <= available.keys():
        raise ValueError("只允许申请当前授权目录中最多4个核心对象")
    existing = set(state["packet"]["selected_objects"])
    if set(names) <= existing:
        return {
            "available": True,
            "already_prepared": True,
            "unchanged": True,
            "prepared_context": state["packet"],
            "message": "所需资料已在prepared_context中；已直接返回原文，不重新检索。业务口径不明确请澄清，不要重复读取。",
        }
    supplement_limit = 8
    if state["supplements"] >= supplement_limit:
        return {
            "available": False,
            "message": "本次核心上下文补充预算已用完；请交付具体业务澄清或受阻原因，不继续扩展。",
        }
    packet = context_packet(workflow, run, ontology, existing | set(names))
    state["supplements"] += 1
    packet["knowledge"] = state["packet"].get("knowledge", [])
    state["packet"] = packet
    workflow.emit(
        run,
        "context.supplemented",
        run.status,
        {
            "summary": "为完成当前需求补齐核心上下文",
            "objects": names,
            "purpose": purpose,
            "reason": reason[:300],
        },
        "context_service",
    )
    return {
        "available": True,
        "prepared_context": packet,
        "message": "核心资料已补齐。资料足够即提交；业务口径不明确则集中澄清，不扩展可选目标。",
    }
