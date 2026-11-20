import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from semora_agent.model import ModelError, TaskSupersededError
from semora_agent.sql_prompts import RESULT_SUMMARY
from semora_contracts import ExecutionResult, QueryRun, RunStatus

from app import result_summary
from app.result_summary import basic_result_description, summary_packet, summarize_result


def setup_case(rows=None, *, truncated=False):
    rows = [[3]] if rows is None else rows
    run = QueryRun(session_id="session", question="有多少符合条件的对象？", result=ExecutionResult(
        columns=["total"], rows=rows, row_count=len(rows), duration_ms=1, truncated=truncated,
    ))
    model = SimpleNamespace(configured=True, request_retries=2, timeout_seconds=300,
                            complete_json=AsyncMock(return_value={"conclusion": "符合条件的对象共有3个。"}))
    events = []
    workflow = SimpleNamespace(model=model, store=SimpleNamespace(sessions={}),
                               emit=lambda run, event, status, data, actor: events.append((event, data)))
    return workflow, run, events


def test_conclusion_uses_actual_rows_and_does_not_modify_shared_model():
    workflow, run, events = setup_case()
    text = asyncio.run(summarize_result(workflow, run))
    assert text == "符合条件的对象共有3个。"
    payload = json.loads(workflow.model.complete_json.await_args.args[1])
    assert payload["result"]["sample_rows"] == [[3]]
    assert payload["result"]["all_returned_rows_included"] is True
    assert "output_contract" in payload and "sql" not in payload
    assert workflow.model.request_retries == 2 and workflow.model.timeout_seconds == 300
    assert workflow.model.complete_json.await_count == 1
    assert events[-1][1]["mode"] == "model"


@pytest.mark.parametrize("response", [{}, {"conclusion": "  "}, {"conclusion": ["invalid"]}])
def test_invalid_conclusion_preserves_result_with_factual_fallback(response):
    workflow, run, events = setup_case()
    workflow.model.complete_json.return_value = response
    assert asyncio.run(summarize_result(workflow, run)) == "本次查询返回 1 条结果。"
    assert run.result.rows == [[3]]
    assert events[-1][1]["mode"] == "basic"


def test_timeout_is_bounded_and_not_retried(monkeypatch):
    workflow, run, events = setup_case()
    async def slow(*args):
        await asyncio.sleep(1)
    workflow.model.complete_json.side_effect = slow
    monkeypatch.setattr(result_summary, "SUMMARY_TIMEOUT_SECONDS", .01)
    assert asyncio.run(summarize_result(workflow, run)) == "本次查询返回 1 条结果。"
    assert workflow.model.complete_json.await_count == 1
    assert any(event == "result.summary.degraded" for event, _ in events)


def test_service_failure_and_truncation_do_not_discard_data():
    workflow, run, _ = setup_case(truncated=True)
    workflow.model.complete_json.side_effect = ModelError("unavailable")
    assert "可能还有符合条件的记录" in asyncio.run(summarize_result(workflow, run))
    assert run.result.rows == [[3]]
    workflow.model.complete_json.side_effect = None
    assert "以上结论仅针对已返回的数据" in asyncio.run(summarize_result(workflow, run))


def test_no_rows_skip_model_but_zero_count_row_is_analyzed():
    workflow, run, _ = setup_case([])
    assert "未找到符合条件的记录" in asyncio.run(summarize_result(workflow, run))
    workflow.model.complete_json.assert_not_awaited()
    workflow, run, _ = setup_case([[0]])
    workflow.model.complete_json.return_value = {"conclusion": "符合条件的对象为0个。"}
    assert "0个" in asyncio.run(summarize_result(workflow, run))
    workflow.model.complete_json.assert_awaited_once()


@pytest.mark.parametrize("count", [100, 101, 1000])
def test_summary_row_limit_keeps_results_and_skips_only_above_100(count):
    workflow, run, events = setup_case([[i] for i in range(count)])
    rows = [list(row) for row in run.result.rows]
    text = asyncio.run(summarize_result(workflow, run))
    assert run.result.rows == rows and run.result.row_count == count
    if count > 100:
        assert text == ""
        workflow.model.complete_json.assert_not_awaited()
        assert [event for event, _ in events] == ["result.summary.skipped"]
    else:
        assert text
        workflow.model.complete_json.assert_awaited_once()


def test_skipped_summary_still_completes_query_without_changing_rows(monkeypatch):
    from test_sql_native import native_harness
    workflow, session, _, _, model, _ = native_harness(monkeypatch)
    # Delivery consumes the final result, independent of SQL engine or UI pagination.
    run = QueryRun(session_id=session.id, question="列出对象", result=ExecutionResult(
        columns=["id"], rows=[[i] for i in range(101)], row_count=101, duration_ms=1))
    workflow.store.runs[run.id] = run
    asyncio.run(workflow._deliver_sql_node({"run_id": run.id}))
    assert run.status == RunStatus.COMPLETED
    assert len(run.result.rows) == 101 and run.answer == ""
    model.complete_json.assert_not_awaited()


def test_large_results_are_bounded_and_scope_limitations_are_explicit():
    workflow, run, _ = setup_case()
    run.question = "需" * 8000
    run.result = ExecutionResult(columns=[f"c{i}" for i in range(30)],
        rows=[["长文本" * 1000] * 30 for _ in range(100)], row_count=100, duration_ms=0)
    packet = summary_packet(workflow, run)
    assert len(json.dumps(packet, ensure_ascii=False).encode()) <= result_summary.PACKET_MAX_BYTES
    assert packet["context_shortened"]
    assert packet["result"]["sample_row_count"] < 100
    assert not packet["result"]["all_returned_rows_included"]
    assert packet["result"]["omitted_columns"] > 0
    assert packet["result"]["long_cells_shortened"]
    assert "sample_rows" in RESULT_SUMMARY and "SQL" in RESULT_SUMMARY


def test_changed_result_does_not_publish_stale_conclusion():
    workflow, run, events = setup_case()
    async def change(*args):
        run.result.rows = [[9]]
        return {"conclusion": "过期结论"}
    workflow.model.complete_json.side_effect = change
    with pytest.raises(TaskSupersededError):
        asyncio.run(summarize_result(workflow, run))
    assert not any(event == "result.summary.completed" for event, _ in events)


def test_full_query_delivers_conclusion_without_changing_sql_or_rows(monkeypatch):
    from test_sql_native import native_harness
    workflow, session, _, _, model, _ = native_harness(monkeypatch)
    program = model.complete_json.return_value
    async def respond(prompt, payload, **kwargs):
        return {"conclusion": "本次查到2个对象。"} if prompt == RESULT_SUMMARY else program
    model.complete_json.side_effect = respond
    run = asyncio.run(workflow.start(session.id, "列出对象", domain="test"))
    assert run.status == RunStatus.COMPLETED, run.error
    assert run.result.rows == [[1], [2]]
    assert run.answer == "本次查到2个对象。"
    assert run.plan.sql_program.steps[0].sql == program["steps"][0]["sql"]
    assert any(event.event == "result.summary.completed" for event in run.events)
