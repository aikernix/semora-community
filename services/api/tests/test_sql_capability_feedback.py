import asyncio
import json
from copy import deepcopy

import pytest

from app import datasources
from app.database_profile import source_profile, validate_features
from app.datasources import DataSourceRecord, DataSourceRepository
from app.executor_errors import ExecutorOperationError
from app.sql_feedback import record_sql_rejection
from semora_contracts.database import normalize_sql_probe
from test_sql_native import native_harness, step
from helpers import query_model_calls


@pytest.mark.parametrize("probe,expected", [
    ({"status": "verified"}, "verified"),
    ({"status": "unavailable", "code": 1064}, "unsupported"),
    ({"status": "unavailable", "sqlstate": "0A000"}, "unsupported"),
    ({"status": "unavailable", "code": 1045}, "unverified"),
    ({"status": "unavailable", "sqlstate": "42501"}, "unverified"),
    ({"status": "unavailable", "code": 3024}, "unverified"),
    ({"status": "unavailable", "code": 2003}, "unverified"),
    ({"status": "unavailable", "code": 942}, "unverified"),
    ({"status": "unavailable"}, "unverified"),
])
def test_isolated_probe_distinguishes_unsupported_from_inconclusive(probe, expected):
    assert normalize_sql_probe(probe, "mysql")["status"] == expected


def test_saved_probe_evidence_survives_serialization_and_controls_authoring():
    import sqlglot
    repo = DataSourceRepository(dsn="")
    repo.save(DataSourceRecord("x", "x", "mysql", "DATASOURCE_X_URL"))
    source = repo.record_test("x", {"read_only": True, "probes": {
        "cte": normalize_sql_probe({"status": "unavailable", "code": 1064}, "mysql"),
        "window_functions": normalize_sql_probe({"status": "unavailable", "code": 3024}, "mysql"),
    }})
    restored = DataSourceRecord("x", "x", "mysql", "DATASOURCE_X_URL", scan_result=json.loads(json.dumps(source.scan_result)))
    profile = source_profile(restored)
    assert profile["features"]["common_table_expressions"] is False
    assert profile["features"]["window_functions"] is None
    with pytest.raises(ValueError, match="已确认不支持"):
        validate_features(sqlglot.parse_one("WITH t AS (SELECT 1 AS n) SELECT n FROM t"), profile)
    validate_features(sqlglot.parse_one("SELECT ROW_NUMBER() OVER()"), profile)


def test_unknown_window_reaches_database_and_executes_when_accepted(monkeypatch):
    program = {"summary": "List identifiers", "steps": [{
        "name": "q", "purpose": "List identifiers", "data_source_id": "source",
        "sql": "SELECT id FROM items ORDER BY ROW_NUMBER() OVER (ORDER BY id)", "output_columns": ["id"],
    }]}
    w, session, execute, estimate, model, _ = native_harness(monkeypatch, program=program)
    run = asyncio.run(w.start(session.id, "List identifiers", domain="test"))
    assert run.status.value == "completed", run.error
    assert len(query_model_calls(model)) == 1
    assert estimate.await_count == execute.await_count == 1
    assert not run.agent_memory.get("sql_repair_count")


def test_database_rejection_is_saved_before_model_repair_without_global_feature_ban(monkeypatch):
    program = {"summary": "List identifiers", "steps": [{
        "name": "q", "purpose": "List identifiers", "data_source_id": "source",
        "sql": "SELECT id FROM items ORDER BY ROW_NUMBER() OVER (ORDER BY id)", "output_columns": ["id"],
    }]}
    w, session, _, estimate, model, _ = native_harness(monkeypatch, program=program)
    model.complete_json.side_effect = [program, {
        "sql": "SELECT id FROM items ORDER BY id", "change_summary": "Preserve identifier order using a direct sort",
    }]
    estimate.side_effect = [ExecutorOperationError("unsupported_sql", "0A000"), {"estimated_rows": 2}]
    run = asyncio.run(w.start(session.id, "List identifiers in order", domain="test"))
    assert run.status.value == "completed", run.error
    source = datasources.datasource_service.repository.get("source")
    feedback = source.scan_result["sql_feedback"]
    assert feedback[0]["scope"] == "statement_only"
    assert feedback[0]["features_used"] == ["window_functions"]
    assert source_profile(source)["features"]["window_functions"] is None
    repair = json.loads(query_model_calls(model)[1].args[1])
    assert repair["data_sources"][0]["sql_feedback"] == feedback
    assert any(e.event == "sql.compatibility.recorded" for e in run.events)


@pytest.mark.parametrize("kind", ["auth", "permission", "connect", "timeout", "transient", "schema"])
def test_non_capability_faults_never_poison_recorded_support(monkeypatch, kind):
    w, session, _, _, _, _ = native_harness(monkeypatch)
    query = step(source="source")
    assert record_sql_rejection(query, None, ExecutorOperationError(kind), "explain") is None
    assert not datasources.datasource_service.repository.get("source").scan_result.get("sql_feedback")


def test_third_repair_can_succeed_without_a_fourth_attempt(monkeypatch):
    w, session, _, estimate, model, _ = native_harness(monkeypatch)
    original = deepcopy(model.complete_json.return_value)
    model.complete_json.side_effect = [original] + [
        {"sql": "SELECT id" + "+0" * n + " AS id FROM items ORDER BY id", "change_summary": f"Correction {n}"}
        for n in range(1, 4)
    ]
    estimate.side_effect = [ExecutorOperationError("syntax", 1064)] * 3 + [{"estimated_rows": 2}]
    run = asyncio.run(w.start(session.id, "List identifiers", domain="test"))
    assert run.status.value == "completed", run.error
    assert run.agent_memory["sql_repair_count"] == [{"attempts": 3}]
    assert len(query_model_calls(model)) == estimate.await_count == 4
    assert run.result.rows == [[1], [2]]


def test_retest_changed_server_discards_old_sql_feedback():
    repo = DataSourceRepository(dsn="")
    item = DataSourceRecord("x", "x", "mysql", "DATASOURCE_X_URL", scan_result={
        "connection": {"version": "5.7"}, "sql_feedback": [{"scope": "statement_only"}],
    })
    repo.save(item)
    repo.record_test("x", {"version": "8.4", "read_only": True})
    assert not item.scan_result.get("sql_feedback")


@pytest.mark.parametrize("dialect,code", [("mysql", 900), ("oracle", 1064), ("postgres", 102), ("", 1064)])
def test_vendor_error_numbers_are_not_reused_for_other_database_products(dialect, code):
    assert normalize_sql_probe({"status": "unavailable", "code": code}, dialect)["status"] == "unverified"


def test_inconclusive_retest_keeps_negative_evidence_until_success_or_target_change():
    repo = DataSourceRepository(dsn="")
    repo.save(DataSourceRecord("x", "x", "mysql", "DATASOURCE_X_URL"))
    source = repo.record_test("x", {"read_only": True, "version": "5.7", "probes": {
        "cte": {"status": "unavailable", "code": 1064},
    }})
    assert source_profile(source)["features"]["common_table_expressions"] is False
    repo.record_test("x", {"read_only": True, "version": "5.7", "probes": {
        "cte": {"status": "unverified", "code": 3024},
    }})
    assert source_profile(source)["features"]["common_table_expressions"] is False
    assert source.scan_result["connection"]["probes"]["cte"]["last_attempt"]["status"] == "unverified"
    repo.record_test("x", {"read_only": True, "version": "5.7", "probes": {"cte": {"status": "verified"}}})
    assert source_profile(source)["features"]["common_table_expressions"] is True


@pytest.mark.parametrize("code", [1305, "42883"])
def test_missing_function_is_recorded_as_statement_evidence_without_disabling_features(monkeypatch, code):
    from types import SimpleNamespace
    native_harness(monkeypatch)
    query = step(source="source", sql="SELECT MISSING_FUNCTION(id) AS id FROM items")
    result = record_sql_rejection(query, SimpleNamespace(physical_sql=query.sql), ExecutorOperationError("schema", code), "explain")
    assert result["functions_used"] == ["MISSING_FUNCTION"]
    assert result["scope"] == "statement_only"
    source = datasources.datasource_service.repository.get("source")
    assert source_profile(source)["features"]["window_functions"] is None


def test_generation_and_database_corrections_share_three_rounds(monkeypatch):
    w, session, _, estimate, model, _ = native_harness(monkeypatch)
    original = deepcopy(model.complete_json.return_value)
    invalid = deepcopy(original)
    invalid["steps"][0]["sql"] = "SELECT id FROM outside_materials"
    model.complete_json.side_effect = [invalid, original] + [
        {"sql": "SELECT id" + "+0" * n + " AS id FROM items ORDER BY id", "change_summary": f"Correction {n}"}
        for n in range(1, 3)
    ]
    estimate.side_effect = [ExecutorOperationError("syntax", 1064)] * 2 + [{"estimated_rows": 2}]
    run = asyncio.run(w.start(session.id, "List identifiers", domain="test"))
    assert run.status.value == "completed", run.error
    assert run.agent_memory["sql_repair_count"] == [{"attempts": 3}]
    assert len(query_model_calls(model)) == 4
