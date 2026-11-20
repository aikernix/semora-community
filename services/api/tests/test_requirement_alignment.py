"""Confirmation replay must preserve facts without repeating finished analysis."""
from helpers import query_model_calls

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

from semora_contracts import RunStatus
from app.control_state import restore_run, run_payload
from app.workflow import Workflow
from test_sql_native import native_harness


def question(name, *, parent=None):
    value = {
        "id": name,
        "field": name,
        "question": "Which scope?",
        "reason": "Scope changes the result",
        "options": [{"id": "all", "value": "ALL_ITEMS", "label": "All items"}],
    }
    if parent:
        value["depends_on"] = [{"question_id": parent, "values": ["ALL_ITEMS"]}]
    return value


def test_direction_then_business_confirmation_survives_workflow_recreation(monkeypatch):
    w, session, execute, estimate, model, runner = native_harness(monkeypatch)
    direction = {
        "summary": "Choose scope",
        "target_state": "needs_direction",
        "questions": [question("focus")],
    }
    business = {"summary": "Confirm period", "questions": [question("period")]}
    runner.side_effect = [(direction, []), (business, [])]

    async def scenario():
        run = await w.start(session.id, "List items", domain="test")
        assert run.status == RunStatus.CLARIFYING
        await w.align(run, "user", {"focus": "all"})
        assert run.status == RunStatus.CLARIFYING
        assert run.requirement_draft.clarification_stage == "business"
        # Restore persisted application state and reconstruct graph functions.
        restored = restore_run(run_payload(run))
        w.store.runs[run.id] = restored
        resumed = Workflow(
            w.store,
            execute=execute,
            estimate=estimate,
            model=model,
            snapshot_loader=w.snapshot_loader,
            checkpointer=w._checkpointer,
        )
        await resumed.align(restored, "user", {"period": "Only the current period"})
        return restored

    run = asyncio.run(scenario())
    assert run.status == RunStatus.COMPLETED, run.error
    assert runner.await_count == 2  # Finished direction analysis was replayed from cache.
    assert len(query_model_calls(model)) == 1
    assert execute.await_count == 1
    assert len(run.alignment_rounds) == len(run.clarification_history) == 2
    cards = [e for e in run.events if e.event == "alignment.required"]
    assert len(cards) == 2  # Resume must not persist either card a second time.
    assert [(f.question_id, f.value) for f in run.requirement.confirmed_facts] == [
        ("focus", "ALL_ITEMS"),
        ("period", "Only the current period"),
    ]
    assert "Only the current period" in query_model_calls(model)[-1].args[1]


def test_hidden_branch_answers_do_not_become_confirmed_facts(monkeypatch):
    root = question("scope")
    root["options"].append({"id": "some", "value": "SOME_ITEMS", "label": "Some items"})
    child = question("detail", parent="scope")
    w, session, _, _, model, _ = native_harness(monkeypatch, questions=[root, child])

    async def scenario():
        run = await w.start(session.id, "List items", domain="test")
        await w.align(run, "user", {"scope": "some", "detail": "stale hidden answer"})
        return run

    run = asyncio.run(scenario())
    assert run.status == RunStatus.COMPLETED, run.error
    assert [(f.question_id, f.value) for f in run.user_answers] == [("scope", "SOME_ITEMS")]
    assert "stale hidden answer" not in query_model_calls(model)[-1].args[1]


def test_answers_are_saved_before_a_followup_model_failure(monkeypatch):
    w, session, execute, _, _, _ = native_harness(monkeypatch, questions=[question("scope")])
    saved = []
    original_save = w.store.save_run

    def save(run):
        saved.append(deepcopy(run_payload(run)))
        original_save(run)

    monkeypatch.setattr(w.store, "save_run", save)

    async def scenario():
        run = await w.start(session.id, "List items", domain="test")

        async def fail(*args):
            assert any(p["user_answers"] and p["status"] == "clarifying" for p in saved)
            raise RuntimeError("model offline")

        w._understand_and_author = AsyncMock(side_effect=fail)
        await w.align(run, "user", {"scope": "User supplied scope"})
        return run

    run = asyncio.run(scenario())
    assert run.status == RunStatus.FAILED
    assert run.user_answers[0].value == "User supplied scope"
    assert run.clarification_history[0]["stage"] == "business"
    assert "model offline" in run.error
    execute.assert_not_awaited()
