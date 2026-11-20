"""Public wording metadata must not change executable names or confirmed choices."""

import asyncio
from semora_contracts import SQLProgram
from app.sql_authoring import CoverageReview, apply_coverage_edits
from test_sql_native import native_harness


def test_business_result_description_and_titles_preserve_sql_and_parameters(monkeypatch):
    program = {
        "summary": "按您确认的范围列出符合条件的对象。",
        "steps": [
            {
                "name": "selected_objects",
                "purpose": "找出符合条件的对象",
                "data_source_id": "source",
                "sql": "SELECT id FROM items WHERE flag=:active_flag",
                "parameters": {"active_flag": 1},
                "output_columns": ["id"],
                "output_labels": {"id": "对象编号"},
            }
        ],
    }
    workflow, session, execute, _, _, _ = native_harness(monkeypatch, program=program)
    run = asyncio.run(workflow.start(session.id, "列出符合条件的对象", domain="test"))
    assert run.status.value == "completed", run.error
    assert run.result.rows == [[1]]
    assert run.answer == "本次查询返回 1 条结果。"
    assert run.plan.sql_program.steps[-1].output_labels == {"id": "对象编号"}
    assert "active_flag" in run.plan.sql_program.steps[-1].parameters
    assert "flag" in execute.await_args.args[0]
    assert "active_flag" not in run.answer and "SELECT" not in run.answer


def test_semantic_revision_keeps_business_explanation_separate_from_sql():
    original = SQLProgram(
        summary="统计对象数量",
        steps=[
            {
                "name": "count_objects",
                "purpose": "统计对象",
                "data_source_id": "source",
                "sql": "SELECT COUNT(*) AS total FROM items WHERE flag=:active_flag",
                "parameters": {"active_flag": 1},
                "output_columns": ["total"],
                "output_labels": {"total": "对象数量"},
            }
        ],
    )
    revision = CoverageReview.model_validate(
        {
            "summary": "统计所选对象的全部记录。",
            "edits": [
                {
                    "step": "count_objects",
                    "sql": "SELECT COUNT(*) AS total FROM items",
                    "parameters": {},
                    "requirement_quote": "全部记录",
                    "change_summary": "将统计范围调整为所选对象的全部记录。",
                }
            ],
        }
    )
    result = apply_coverage_edits(original, revision)
    assert result.summary == "统计所选对象的全部记录。"
    assert result.steps[0].purpose == revision.edits[0].change_summary
    assert result.steps[0].sql == "SELECT COUNT(*) AS total FROM items"
    assert result.steps[0].output_columns == ["total"]
    assert result.steps[0].output_labels == {"total": "对象数量"}
    assert original.steps[0].parameters == {"active_flag": 1}


def test_legacy_or_invalid_display_titles_do_not_fail_a_valid_query():
    value = {
        "summary": "列出对象",
        "steps": [
            {
                "name": "q",
                "purpose": "列出对象",
                "data_source_id": "source",
                "sql": "SELECT id FROM items",
                "output_columns": ["id"],
            }
        ],
    }
    assert SQLProgram.model_validate(value).steps[0].output_labels == {}
    value["steps"][0]["output_labels"] = {"other": "其他对象"}
    assert SQLProgram.model_validate(value).steps[0].output_labels == {}
    value["steps"][0]["output_labels"] = {"id": "对象编号", "other": "其他对象", "bad": 123}
    assert SQLProgram.model_validate(value).steps[0].output_labels == {"id": "对象编号"}
    value["steps"][0]["output_labels"] = None
    assert SQLProgram.model_validate(value).steps[0].output_labels == {}


def test_copy_edit_only_changes_public_text_and_keeps_branch_values():
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from app.business_copy import polish_public_text
    from app.sql_authoring import Understanding
    from semora_contracts import QueryRun

    report = Understanding(
        summary="从RAW_EVENT表统计记录",
        questions=[
            {
                "id": "scope",
                "field": "RAW_EVENT.status",
                "question": "希望统计哪些对象？",
                "reason": "需要读取RAW_EVENT表。",
                "options": [
                    {
                        "id": "active",
                        "value": "STATUS_01",
                        "label": "status_code=1",
                        "description": "当前有效的对象",
                    }
                ],
            },
            {
                "id": "time",
                "field": "RAW_EVENT.created_at",
                "question": "从哪天开始？",
                "reason": "时间会影响范围。",
                "depends_on": [{"question_id": "scope", "values": ["STATUS_01"]}],
            },
        ],
    )
    model = SimpleNamespace(
        complete_json=AsyncMock(
            return_value={
                "texts": {
                    "summary": "统计当前有效的对象。",
                    "questions.0.reason": "选择会影响纳入统计的对象。",
                    "questions.0.options.0.label": "当前有效的对象",
                    "questions.0.options.0.value": "CHANGED",
                    "questions.1.question": "未经请求的改动",
                }
            }
        )
    )
    workflow = SimpleNamespace(model=model, emit=Mock())
    run = QueryRun(session_id="s", domain="generic", question="统计对象")
    asyncio.run(polish_public_text(workflow, run, report))
    assert report.summary == "统计当前有效的对象。"
    assert report.questions[0].options[0].label == "当前有效的对象"
    assert report.questions[0].options[0].value == "STATUS_01"
    assert report.questions[0].field == "RAW_EVENT.status"
    assert report.questions[1].question == "从哪天开始？"
    assert report.questions[1].depends_on[0].values == ["STATUS_01"]
    payload = json.loads(model.complete_json.await_args.args[1])
    assert "STATUS_01" not in json.dumps(payload)
    assert model.complete_json.await_count == 1


def test_copy_edit_service_failure_does_not_fail_the_query():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from app.business_copy import polish_public_text
    from app.sql_authoring import StepRepair
    from semora_contracts import QueryRun

    value = StepRepair(sql="SELECT id FROM items", change_summary="调整SQL后保持范围不变")
    workflow = SimpleNamespace(
        model=SimpleNamespace(complete_json=AsyncMock(side_effect=RuntimeError("offline"))),
        emit=Mock(),
    )
    run = QueryRun(session_id="s", domain="generic", question="列出对象")
    assert asyncio.run(polish_public_text(workflow, run, value)) is value
    assert value.sql == "SELECT id FROM items"
    assert workflow.emit.call_args.args[1] == "copy.unavailable"
