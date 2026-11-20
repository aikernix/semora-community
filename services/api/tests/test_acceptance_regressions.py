"""Generic regressions from real query acceptance; no business-specific schema."""
from helpers import query_model_calls

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlglot

from app import datasources, prepared_context
from app.database_profile import source_profile, validate_features
from app.sql_dialect import compile_step
from app.sql_errors import SQLIssue
from semora_contracts import ConfirmedFact, QueryRun
from test_sql_native import native_harness, sources as source_fixture, step


@pytest.mark.parametrize(
    "version,supported",
    [
        ("5.7.21-log", None),
        ("8.0.45", None),
        ("10.1.48-MariaDB", None),
        ("5.5.5-10.6.12-MariaDB", None),
        ("", None),
        ("5.7.25-TiDB-v7.5", None),
    ],
)
def test_scanned_capabilities_respect_vendor_and_unknowns(version, supported):
    profile = source_profile(
        SimpleNamespace(type="mysql", scan_result={"connection": {"version": version}})
    )
    assert profile["features"].get("window_functions") is supported
    assert profile["features"].get("common_table_expressions") is supported


def test_mysql_aggregate_alias_is_expanded_without_losing_null_order(sources):
    import duckdb

    query = step(
        source="b",
        sql="SELECT item_id,MAX(kind) AS latest FROM events GROUP BY item_id ORDER BY latest DESC LIMIT 2",
    )
    artifact = compile_step(query, sources)
    assert "CASE WHEN MAX(kind) IS NULL" in artifact.physical_sql
    assert "CASE WHEN latest IS NULL" not in artifact.physical_sql
    db = duckdb.connect()
    db.execute("CREATE TABLE events(item_id INTEGER,kind INTEGER)")
    db.execute("INSERT INTO events VALUES (1,3),(1,8),(2,NULL),(3,7)")
    adapted = sqlglot.transpile(artifact.physical_sql, read="mysql", write="duckdb")[0]
    baseline = sqlglot.transpile(query.sql, read="postgres", write="duckdb")[0]
    assert db.execute(adapted).fetchall() == db.execute(baseline).fetchall() == [(2, None), (1, 8)]
    db.close()


def test_aggregate_alias_resolution_stays_inside_its_select_scope(sources):
    sql = "SELECT x.item_id,x.latest FROM (SELECT item_id,MAX(kind) AS latest FROM events GROUP BY item_id ORDER BY latest DESC LIMIT 2) x ORDER BY x.latest DESC"
    artifact = compile_step(step(source="b", sql=sql), sources)
    assert (
        "MAX(kind)" in artifact.physical_sql
        and "CASE WHEN x.latest IS NULL" in artifact.physical_sql
    )


def test_known_unsupported_construct_is_corrected_before_database_call(monkeypatch):
    w, session, execute, estimate, model, _ = native_harness(monkeypatch)
    source = datasources.datasource_service.repository.get("source")
    source.type = "mysql"
    source.scan_result = {"connection": {"version": "5.7.21-log", "probes": {"window_functions": {"status": "unsupported", "code": 1064}}}}
    correct = deepcopy(model.complete_json.return_value)
    invalid = deepcopy(correct)
    invalid["steps"][0]["sql"] = "SELECT id,ROW_NUMBER() OVER(ORDER BY id) AS n FROM items"
    model.complete_json.side_effect = [invalid, correct]
    run = asyncio.run(w.start(session.id, "List identifiers", domain="test"))
    assert run.status.value == "completed", run.error
    assert len(query_model_calls(model)) == 2
    assert "5.7.21-log" in query_model_calls(model)[-1].args[1]
    assert estimate.await_count == execute.await_count == 1


def test_capability_error_is_specific_not_a_missing_table():
    with pytest.raises(SQLIssue, match="已确认不支持窗口函数"):
        validate_features(
            sqlglot.parse_one("SELECT ROW_NUMBER() OVER()"),
            {"vendor": "mysql", "version": "5.7", "features": {"window_functions": False}},
        )


def test_free_text_answers_rebuild_context_and_allow_supplement(monkeypatch):
    from helpers import harness

    w, session, _, _ = harness(monkeypatch)
    run = QueryRun(session_id=session.id, domain="test", question="List items")
    _, ontology = asyncio.run(w.snapshot_loader("test"))
    ontology["objects"].append({"name": "event", "table": "events", "properties": []})
    ontology["relations"] = [
        {"name": "has_events", "from": "item", "to": "event", "condition": "item.id=event.item_id"}
    ]
    w.model = SimpleNamespace(complete_json=AsyncMock(return_value={"objects": ["item"]}))
    first = asyncio.run(prepared_context.prepare_context(w, run, run.question, ontology, {}))
    run.user_answers = [
        ConfirmedFact(
            question_id="scope", field="scope", value="Include events", label="Include events"
        )
    ]
    prepared_context.confirmed_fact_context(w, run)
    second = asyncio.run(prepared_context.prepare_context(w, run, run.question, ontology, {}))
    assert first["binding"] != second["binding"]
    assert len(query_model_calls(w.model)) == 2
    supplement = prepared_context.supplement_context(
        w, run, ontology, second, ["event"], "output", "Need related records", {}
    )
    assert "event" in supplement["prepared_context"]["selected_objects"]
    assert (
        asyncio.run(prepared_context.prepare_context(w, run, run.question, ontology, {})) is second
    )


def test_confirmation_summary_uses_final_program_not_initial_draft(monkeypatch):
    w, session, *_ = native_harness(monkeypatch)
    w.model.complete_json.return_value["summary"] = "Final confirmed scope"
    run = asyncio.run(w.start(session.id, "List identifiers", domain="test"))
    assert run.requirement.summary == "Final confirmed scope"
    assert run.requirement.analysis_steps == ["List items"]


def test_error_codes_and_safe_mysql_syntax_location():
    from services.executor.app.errors import database_error

    error = database_error(
        Exception(1247, "Reference 'latest' not supported (reference to group function)"),
        "estimate",
    )
    assert (
        error.detail["error_type"] == "syntax"
        and "aggregate" in error.detail["diagnostic"]["message"]
    )
    error = database_error(
        Exception(
            1064,
            "You have an error in your SQL syntax; check manual near 'OVER (PARTITION BY private_name ORDER BY secret_value)' at line 2",
        ),
        "estimate",
    )
    assert error.detail["diagnostic"]["sql_tokens"] == ["OVER", "PARTITION", "BY", "ORDER"]
    assert "secret_value" not in str(error.detail) and "private_name" not in str(error.detail)


@pytest.fixture
def sources(monkeypatch):
    return source_fixture.__wrapped__(monkeypatch)


def test_sorted_derived_group_does_not_silently_pick_an_arbitrary_row(sources):
    sql = "SELECT x.item_id,x.kind FROM (SELECT item_id,kind FROM events ORDER BY kind DESC) x GROUP BY x.item_id"
    with pytest.raises(SQLIssue, match="非聚合输出列"):
        compile_step(step(source="b", sql=sql), sources)
    valid = "SELECT x.item_id,MAX(x.kind) AS kind FROM (SELECT item_id,kind FROM events) x GROUP BY x.item_id"
    assert compile_step(step(source="b", sql=valid), sources)


def test_primary_key_grouping_and_qualified_group_names_are_preserved(sources):
    assert compile_step(step(sql="SELECT I.id,I.amount FROM items I GROUP BY I.id"), sources)
    assert compile_step(
        step(source="b", sql="SELECT E.item_id,COUNT(*) FROM events E GROUP BY item_id"), sources
    )


def test_mutable_session_variable_ranking_is_not_a_portable_readonly_query(sources):
    with pytest.raises(SQLIssue, match="会话变量"):
        compile_step(
            step(source="b", sql="SELECT item_id,@n := @n + 1 AS rank FROM events"), sources
        )


def test_explicit_ownership_use_is_loaded_even_if_missing_from_object_list(monkeypatch):
    from helpers import harness
    import json

    w, session, _, _ = harness(monkeypatch)
    run = QueryRun(
        session_id=session.id, domain="test", question="Summarize items by the owning group"
    )
    _, ontology = asyncio.run(w.snapshot_loader("test"))
    ontology["objects"].append(
        {"name": "group", "label": "Owning group", "table": "groups", "properties": []}
    )
    ontology["relations"] = [{"name": "owned_by", "from": "item", "to": "group"}]
    w.model = SimpleNamespace(
        complete_json=AsyncMock(
            return_value={
                "objects": ["item"],
                "object_uses": [{"phrase": "owning group", "object": "group"}],
            }
        )
    )
    saved = asyncio.run(prepared_context.prepare_context(w, run, run.question, ontology, {}))
    assert set(saved["packet"]["selected_objects"]) == {"item", "group"}
    request = json.loads(query_model_calls(w.model)[-1].args[1])
    assert request["relation_catalog"]["rows"] == [["owned_by", "item", "group"]]
    assert all("properties" not in item for item in request["object_catalog"])


def test_relevant_field_hints_are_bounded_and_do_not_expose_full_schema():
    objects = {
        "a": {
            "name": "a",
            "label": "拥有方",
            "properties": [
                {"name": "code", "label": "所属区域"},
                {"name": "private", "label": "未提到的属性"},
            ],
        },
        "b": {
            "name": "b",
            "properties": [{"name": "id", "label": "id"}, {"name": "status", "aliases": "State"}],
        },
    }
    hints = prepared_context.field_candidates(
        objects, "按拥有方的所属区域统计已 paid 的记录，State 分开"
    )
    assert hints == [
        {"object": "a", "field": "code", "label": "所属区域"},
        {"object": "b", "field": "status", "label": "State"},
    ] or hints == [
        {"object": "b", "field": "status", "label": "State"},
        {"object": "a", "field": "code", "label": "所属区域"},
    ]
    assert len(prepared_context.field_candidates(objects, "所属区域 State", limit=1)) == 1


def test_sql_contract_keeps_original_user_authority_separate_from_model_interpretation(monkeypatch):
    import json

    w, session, _, _, model, runner = native_harness(monkeypatch)
    runner.return_value = (
        {"summary": "UNCONFIRMED WRONG SCOPE", "skeleton": ["Only flagged rows"]},
        [],
    )
    run = asyncio.run(w.start(session.id, "All records of matching entities", domain="test"))
    assert run.status.value == "completed"
    payload = json.loads(query_model_calls(model)[-1].args[1])
    assert payload["original_request"] == "All records of matching entities"
    assert payload["request"]["summary"] == "UNCONFIRMED WRONG SCOPE"
    assert payload["request"]["sequence"] == ["Only flagged rows"]
    assert 'analysis_steps' not in payload['request']
    assert payload["confirmed_answers"] == []
    from semora_agent.sql_prompts import AUTHOR
    assert "不是额外的用户确认" in AUTHOR
    assert "冲突时以用户明确口径为准" in AUTHOR


def test_independent_coverage_correction_preserves_all_records_of_selected_entities(monkeypatch):
    from semora_agent.sql_prompts import COVERAGE

    base = "WITH records AS (SELECT 1 AS entity_id,5 AS amount,1 AS flag UNION ALL SELECT 1,7,0 UNION ALL SELECT 2,20,0) "
    bad = (
        base + "SELECT entity_id,SUM(amount) AS total FROM records WHERE flag=1 GROUP BY entity_id"
    )
    good = (
        base
        + "SELECT r.entity_id,SUM(r.amount) AS total FROM records r WHERE EXISTS (SELECT 1 FROM records q WHERE q.entity_id=r.entity_id AND q.flag=1) GROUP BY r.entity_id"
    )
    program = {
        "summary": "Aggregate records",
        "steps": [
            {
                "name": "q",
                "purpose": "Aggregate",
                "data_source_id": "source",
                "sql": bad,
                "output_columns": ["entity_id", "total"],
            }
        ],
    }
    corrected = deepcopy(program)
    corrected["steps"][0]["sql"] = good
    w, session, _, _, model, _ = native_harness(monkeypatch, program=program)
    datasources.datasource_service.repository.get("source").scan_result={"capabilities":{"cte":{"status":"verified"}}}
    model.complete_json.side_effect = [
        program,
        {
            "edits": [
                {
                    "step": "q",
                    "sql": good,
                    "requirement_quote": "all records",
                    "change_summary": "Include unflagged records of selected entities",
                }
            ],
        },
    ]
    run = asyncio.run(
        w.start(session.id, "Total all records of entities with a flagged record", domain="test")
    )
    assert run.status.value == "completed", run.error
    assert run.result.rows == [[1, 12]]
    assert len(query_model_calls(model)) == 2
    assert query_model_calls(model)[-1].args[0] == COVERAGE
    assert not run.agent_memory.get("sql_repair_count")


def test_unavailable_consistency_check_does_not_discard_executable_sql(monkeypatch):
    from semora_agent.model import ModelError

    program = {
        "summary": "Total amount",
        "steps": [
            {
                "name": "q",
                "purpose": "Sum",
                "data_source_id": "source",
                "sql": "SELECT SUM(amount) AS total FROM items",
                "output_columns": ["total"],
            }
        ],
    }
    w, session, _, _, model, _ = native_harness(monkeypatch, program=program)
    model.complete_json.side_effect = [program, ModelError("model transport timeout")]
    run = asyncio.run(w.start(session.id, "Total amount", domain="test"))
    assert run.status.value == "completed", run.error
    assert run.result.rows == [[25]] and run.delivery_warnings
    assert len(query_model_calls(model)) == 2


def test_light_identity_catalog_keeps_entity_keys_separate_from_selected_fields(monkeypatch):
    from helpers import harness

    w, session, _, _ = harness(monkeypatch)
    run = QueryRun(
        session_id=session.id, domain="test", question="Group matching owners by category"
    )
    _, ontology = asyncio.run(w.snapshot_loader("test"))
    ontology["objects"].append(
        {
            "name": "owner",
            "label": "Owner",
            "table": "owners",
            "primaryKey": "owner_id",
            "properties": [{"name": "owner_id"}, {"name": "category"}],
        }
    )
    packet = prepared_context.context_packet(w, run, ontology, ["item"])
    assert packet["selected_objects"] == ["item"]
    assert {"object": "owner", "label": "Owner", "primaryKey": "owner_id"} in packet[
        "identity_catalog"
    ]
    assert all(set(row) == {"object", "label", "primaryKey"} for row in packet["identity_catalog"])
