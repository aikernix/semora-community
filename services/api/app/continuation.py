"""Explicit business handoff between attempts; never transfer execution authority."""

from __future__ import annotations

import json


def legacy_confirmations(run):
    # Older runs did not persist answers separately. Only inspect framework-built
    # Human messages; never promote model messages or tool output to user facts.
    for item in reversed(run.agent_memory.get("analyst", [])):
        if item.get("type") != "human":
            continue
        try:
            request = json.loads(item.get("data", {}).get("content", "")).get("request", "")
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(request, str) and "。用户补充确认：" in request:
            return request.rsplit("。用户补充确认：", 1)[1]
    return ""


def handoff(source, session):
    previous = source.continuation or {}
    facts = {f["question_id"]: f for f in previous.get("confirmed_facts", [])}
    if source.requirement:
        facts.update(
            {f.question_id: f.model_dump(mode="json") for f in source.requirement.confirmed_facts}
        )
    for round in source.alignment_rounds:
        for fact in round.get("answers", []):
            if fact.get("question_id"):
                facts[fact["question_id"]] = fact
    facts.update({f.question_id: f.model_dump(mode="json") for f in source.user_answers})
    counts = dict(previous.get("role_rounds", {}))
    for event in source.events:
        if event.event == "agent.model.started":
            role = event.data.get("agent", "analyst")
            counts[role] = counts.get(role, 0) + 1
    draft = (
        source.requirement_draft.model_dump(mode="json")
        if source.requirement_draft
        else previous.get("draft")
    )
    plan = source.plan or source.proposed_plan or previous.get("candidate_plan")
    if hasattr(plan, "model_dump"):
        plan = plan.model_dump(mode="json")
    history = previous.get("question_history", []) + [source.question]
    return {
        "clarification_history": list(
            {
                item["key"]: item
                for item in [
                    *previous.get("clarification_history", []),
                    *source.clarification_history,
                ]
            }.values()
        ),
        "source_run_id": source.id,
        "root_run_id": previous.get("root_run_id", source.id),
        "question_history": [history[0], *history[-11:]] if len(history) > 12 else history,
        "requirement_form": previous.get(
            "requirement_form", {} if source.message_mode == "new" else session.requirement_form
        ),
        "confirmed_facts": list(facts.values()),
        "confirmed_fact_context": source.agent_memory.get("confirmed_fact_context")
        or previous.get("confirmed_fact_context", []),
        "legacy_confirmations": legacy_confirmations(source)
        or previous.get("legacy_confirmations", ""),
        "draft": draft,
        "candidate_plan": plan,
        "query_rehearsal": source.query_rehearsal.model_dump(mode="json")
        if source.query_rehearsal
        else previous.get("query_rehearsal"),
        "confirmed_requirement": source.requirement.model_dump(mode="json")
        if source.requirement
        else previous.get("confirmed_requirement"),
        "previous_error": source.error,
        "failed_stage": next(
            (e.status.value for e in reversed(source.events) if e.status.value != "failed"),
            "received",
        ),
        "model_round_offset": sum(counts.values()),
        "role_rounds": counts,
        "ontology": source.ontology.model_dump(mode="json") if source.ontology else None,
        "evidence_origin_at": previous.get("evidence_origin_at", source.created_at.isoformat()),
    }
