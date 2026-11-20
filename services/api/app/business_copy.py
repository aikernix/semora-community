"""One optional copy edit of public strings; executable fields never leave here."""

import asyncio
import json
import re
from pydantic import BaseModel, ConfigDict, Field
from semora_agent.model import TaskSupersededError
from semora_agent.sql_prompts import BUSINESS_COPY
from .run_state import revision


class CopyEdits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    texts: dict[str, str] = Field(default_factory=dict)


TECHNICAL = re.compile(
    r"字段|主键|外键|数据库|表结构|预聚合|聚合|粒度|维度|(?<!报)表(?=中|里|进行|统计|查询|[，。；、\s]|$)|"
    r"(?<![A-Za-z0-9_])(?:SQL|SELECT|WHERE|JOIN|GROUP\s+BY|EXISTS)(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])(?:COUNT|SUM|AVG|MAX|MIN)\s*\(|"
    r"(?i:(?<![A-Za-z0-9_])(?:SQLGlot|DuckDB|PostgreSQL|MySQL)(?![A-Za-z0-9_]))|"
    r"(?<![A-Za-z0-9_])[A-Za-z_]\w*\.[A-Za-z_]\w*(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*\s*[=<>]",
)


def public_fields(value):
    fields = {}

    def add(path, owner, key):
        text = owner[key] if isinstance(owner, (list, dict)) else getattr(owner, key)
        if isinstance(text, str) and text:
            fields[path] = (owner, key, text)

    for name in ("summary", "change_summary"):
        if hasattr(value, name):
            add(name, value, name)
    for i, _ in enumerate(getattr(value, "skeleton", [])):
        add(f"skeleton.{i}", value.skeleton, i)
    if getattr(value, "confidence", None):
        add("confidence.reason", value.confidence, "reason")
    for i, q in enumerate(getattr(value, "questions", [])):
        for name in ("question", "reason"):
            add(f"questions.{i}.{name}", q, name)
        for j, o in enumerate(q.options):
            for name in ("label", "description"):
                add(f"questions.{i}.options.{j}.{name}", o, name)
    for i, step in enumerate(getattr(value, "steps", [])):
        add(f"steps.{i}.purpose", step, "purpose")
        for j, key in enumerate(step.output_labels):
            add(f"steps.{i}.titles.{j}", step.output_labels, key)
    return fields


async def polish_public_text(workflow, run, value):
    fields = public_fields(value)
    packet = (run.agent_memory.get("prepared_context") or [{}])[-1].get("packet", {})
    names = {
        str(p.get("column") or p.get("name") or "")
        for o in packet.get("objects", [])
        for p in o["object"].get("properties", [])
    }
    names.update(o["object"]["name"] for o in packet.get("objects", []))
    names = {
        n
        for n in names
        if len(n) >= 4
        and (n != n.lower() or "_" in n)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)
    }
    identifiers = (
        re.compile(
            r"(?<![A-Za-z0-9_])(?:"
            + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            + r")(?![A-Za-z0-9_])",
            re.I,
        )
        if names
        else None
    )
    needs = lambda text: bool(TECHNICAL.search(text) or identifiers and identifiers.search(text))
    flagged = {path: text for path, (_, _, text) in fields.items() if needs(text)}
    if not flagged:
        return value
    initial = revision(run)
    try:
        raw = await asyncio.wait_for(
            workflow.model.complete_json(
                BUSINESS_COPY,
                json.dumps(
                    {
                        "texts": flagged,
                        "context": {path: text for path, (_, _, text) in fields.items()},
                        "output_contract": CopyEdits.model_json_schema(),
                    },
                    ensure_ascii=False,
                ),
            ),
            timeout=20,
        )
        if revision(run) != initial:
            raise TaskSupersededError("需求已更新，丢弃过期文案")
        edits = CopyEdits.model_validate(raw)
    except TaskSupersededError:
        raise
    except Exception as exc:
        workflow.emit(
            run,
            "copy.unavailable",
            run.status,
            {"summary": "文案校对暂不可用，保留原意", "error_type": type(exc).__name__},
            "copy_editor",
        )
        return value
    changed = 0
    for path, text in edits.texts.items():
        if (
            path not in flagged
            or not text.strip()
            or len(text) > max(1200, len(flagged[path]) * 3)
            or needs(text)
        ):
            continue
        owner, key, old = fields[path]
        if isinstance(owner, (list, dict)):
            owner[key] = text.strip()
        else:
            setattr(owner, key, text.strip())
        changed += text.strip() != old
    if changed:
        workflow.emit(
            run,
            "copy.updated",
            run.status,
            {"summary": "已将说明调整为业务表达", "fields": changed},
            "copy_editor",
        )
    return value
