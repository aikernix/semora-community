import asyncio
import json
from copy import deepcopy

from semora_contracts import RequirementDraft, ConfirmedFact
from app import sql_authoring
from app.executor_errors import ExecutorOperationError
from helpers import query_model_calls
from test_sql_native import native_harness


def test_sql_request_includes_complete_rules_but_not_pending_questions(monkeypatch):
    answers = [{"field": "range", "value": "all", "label": "All records"}]
    monkeypatch.setattr(sql_authoring, "context", lambda *args: {
        "request": args[-1], "confirmed_answers": deepcopy(answers), "materials": {},
    })
    draft = RequirementDraft(
        summary="Summarize all records of selected entities",
        analysis_steps=["Select the latest entities", "Then check their complete history"],
        subject="Entity", time_semantics="Current period", grain="One row per entity",
        data_scope={"region": "selected"}, output_fields=["count"],
        aggregation_rules=["Count every entity once"], ordering=["Latest first"],
        assumptions=["No additional state restriction"],
    )
    before = draft.model_dump(mode="json")
    payload = sql_authoring.sql_context(None, None, "Original user text", draft)
    assert payload["original_request"] == "Original user text"
    for key in ("summary", "analysis_steps", "subject", "time_semantics", "grain",
                "data_scope", "output_fields", "aggregation_rules", "ordering", "assumptions"):
        assert payload["request"][key] == before[key]
    assert payload["confirmed_answers"] == answers
    assert not {"clarifications", "technical_gaps", "confidence", "status", "evidence"} & payload["request"].keys()
    assert draft.model_dump(mode="json") == before


def test_repair_reuses_author_contract_and_user_supplements(monkeypatch):
    w, session, _, estimate, model, _ = native_harness(monkeypatch)
    program = deepcopy(model.complete_json.return_value)
    model.complete_json.side_effect = [program, {
        "sql": "SELECT id+0 AS id FROM items", "parameters": {},
        "change_summary": "Correct the expression without changing the requested scope",
    }]
    estimate.side_effect = [ExecutorOperationError("syntax", 1064), {"estimated_rows": 2}]
    run = asyncio.run(w.start(session.id, "List all identifiers", domain="test"))
    assert run.status.value == "completed", run.error
    calls = query_model_calls(model)
    author = json.loads(calls[0].args[1])
    repair = json.loads(calls[1].args[1])
    assert isinstance(author["request"], dict)
    assert repair["request"] == author["request"]
    assert repair["original_request"] == author["original_request"]
    assert repair["confirmed_answers"] == author["confirmed_answers"]
    from app.sql_input_compaction import compact_sql_input
    saved=run.agent_memory["sql_request_context"][-1]["requirement_contract"]
    assert compact_sql_input({'request':saved})['request']==author['request']
    assert 'analysis_steps' in saved and 'analysis_steps' not in author['request']


def test_user_free_text_and_selected_option_meaning_are_in_sql_contract_context(monkeypatch):
    from helpers import harness
    from semora_contracts import QueryRun
    w, session, _, _ = harness(monkeypatch)
    run = QueryRun(session_id=session.id, domain="test", question="Summarize records")
    run.user_answers = [ConfirmedFact(question_id="q1", field="scope", value="Use all records, including unmatched ones", label="Free text")]
    payload = sql_authoring.sql_context(w, run, run.question, RequirementDraft(summary="Summarize records"))
    assert payload["confirmed_answers"][0]["value"] == "Use all records, including unmatched ones"
    assert payload["request"]["summary"] == "Summarize records"


def test_understanding_rules_reach_author_and_survive_persistence(monkeypatch):
    from app.control_state import run_payload, restore_run
    w, session, _, _, model, runner = native_harness(monkeypatch)
    rules = {
        "subject": "Requested entities", "time_semantics": "Current period",
        "grain": "One row per entity", "data_scope": {"range": "all"},
        "output_fields": ["Entity identifier"], "metrics": ["Entity count"],
        "dimensions": ["Entity"], "filters": ["Use the requested period"],
        "aggregation_rules": ["Count each entity once"], "ordering": ["Identifier ascending"],
        "sequence": ["Select entities", "Then return identifiers"],
        "assumptions": ["No additional state restriction"],
    }
    runner.return_value = ({"summary": "List requested identifiers", "target_state": "known",
                            "skeleton": ["Return each entity once"], "requirements": rules}, [])
    run = asyncio.run(w.start(session.id, "List identifiers", domain="test"))
    assert run.status.value == "completed", run.error
    author = json.loads(query_model_calls(model)[0].args[1])
    for key, value in rules.items():
        assert author["request"][key] == value
    restored = restore_run(run_payload(run))
    assert restored.requirement.aggregation_rules == rules["aggregation_rules"]
    assert restored.requirement.sequence == rules["sequence"]
    from app.sql_input_compaction import compact_sql_input
    saved=restored.agent_memory["sql_request_context"][-1]["requirement_contract"]
    assert compact_sql_input({'request':saved})['request']==author['request']


def test_legacy_or_null_business_rules_do_not_break_old_understanding():
    from semora_contracts import QueryUnderstanding
    for value in ({}, {"requirements": None}, {"requirements": {"filters": None, "subject": None, "data_scope": None}}):
        report = QueryUnderstanding.model_validate({"summary": "List records", "target_state": "known", **value})
        assert report.requirements.filters == []
        assert report.requirements.subject == ""
        assert report.requirements.data_scope == {}
