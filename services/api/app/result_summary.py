"""Bounded post-query conclusions; unavailable narration never discards results."""

import asyncio
from copy import copy
import json

from pydantic import BaseModel, ConfigDict, Field
from semora_agent.model import TaskSupersededError
from semora_agent.sql_prompts import RESULT_SUMMARY
from semora_contracts import RunStatus

from .run_state import revision


SUMMARY_TIMEOUT_SECONDS = 20
SUMMARY_MAX_ROWS = 100
PACKET_MAX_BYTES = 32_000


class ResultConclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conclusion: str = Field(min_length=1, max_length=500)


def basic_result_description(result):
    if not result.row_count:
        return "按已确认条件查询，未找到符合条件的记录。"
    text = f"本次查询返回 {result.row_count:,} 条结果。"
    if result.truncated:
        text += "结果已达到返回上限，可能还有符合条件的记录；以上仅反映本次返回的数据。"
    return text


def summary_packet(workflow, run):
    result = run.result
    program = run.plan.sql_program if run.plan else None
    labels = program.steps[-1].output_labels if program and program.steps else {}
    session = workflow.store.sessions.get(run.session_id)
    original = (session.requirement_form.get("objective") if session else None) or (
        run.continuation.question_history[0]
        if run.continuation and run.continuation.question_history
        else run.question
    )
    scope = run.requirement or run.requirement_draft
    packet = {
        "request": original[:3000],
        "confirmed_scope": (scope.summary if scope else "")[:2000],
        "query_description": (run.sql_explanation or "")[:1500],
        "context_shortened": len(original) > 3000 or bool(scope and len(scope.summary) > 2000)
        or len(run.sql_explanation or "") > 1500,
        "result": {
            "returned_row_count": result.row_count,
            "result_truncated": result.truncated,
            "column_count": len(result.columns),
            "columns": [
                {"name": c[:160], "label": (labels.get(c) or c)[:160]}
                for c in result.columns[:20]
            ],
            "omitted_columns": max(0, len(result.columns) - 20),
            "sample_rows": [],
            "sample_row_count": 0,
            "all_returned_rows_included": False,
            "long_cells_shortened": False,
        },
        "output_contract": ResultConclusion.model_json_schema(),
    }
    data = packet["result"]
    while len(data["columns"]) > 1 and len(json.dumps(packet, ensure_ascii=False).encode()) > 28_000:
        data["columns"].pop()
    data["omitted_columns"] = len(result.columns) - len(data["columns"])
    for row in result.rows[:40]:
        cells = []
        for value in row[:len(data["columns"])]:
            if value is None or isinstance(value, (bool, int, float)):
                cells.append(value)
            else:
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
                data["long_cells_shortened"] |= len(text) > 160
                cells.append(text[:160])
        data["sample_rows"].append(cells)
        if len(json.dumps(packet, ensure_ascii=False).encode()) > PACKET_MAX_BYTES:
            data["sample_rows"].pop()
            break
    data["sample_row_count"] = len(data["sample_rows"])
    data["all_returned_rows_included"] = data["sample_row_count"] == len(result.rows) == result.row_count
    return packet


async def summarize_result(workflow, run):
    initial = revision(run)
    if run.result.row_count > SUMMARY_MAX_ROWS:
        if run.agent_memory.get("user_cancelled"):
            raise TaskSupersededError("查询已取消，不生成结果说明")
        workflow.emit(run, "result.summary.skipped", RunStatus.EXPLAINING,
                      {"summary": "返回结果超过100条，跳过结果说明",
                       "reason": "row_count_limit", "row_count": run.result.row_count,
                       "limit": SUMMARY_MAX_ROWS}, "result_summarizer")
        return ""
    fallback = basic_result_description(run.result)
    text, mode = fallback, "basic"
    if run.result.rows and workflow.model.configured:
        workflow.emit(run, "result.summary.started", RunStatus.EXPLAINING,
                      {"summary": "正在根据实际查询结果整理业务结论"}, "result_summarizer")
        try:
            # The shared model configuration is unchanged; this optional call
            # has one attempt and its own short deadline, without a ReAct loop.
            model = copy(workflow.model)
            model.request_retries = 0
            model.timeout_seconds = SUMMARY_TIMEOUT_SECONDS
            raw = await asyncio.wait_for(
                model.complete_json(RESULT_SUMMARY, json.dumps(summary_packet(workflow, run), ensure_ascii=False)),
                timeout=SUMMARY_TIMEOUT_SECONDS,
            )
            conclusion = ResultConclusion.model_validate(raw).conclusion.strip()
            if not conclusion:
                raise ValueError("empty conclusion")
            text, mode = conclusion, "model"
            if run.result.truncated:
                text += " 本次结果已达到返回上限，以上结论仅针对已返回的数据。"
        except TaskSupersededError:
            raise
        except Exception as exc:
            workflow.emit(run, "result.summary.degraded", RunStatus.EXPLAINING,
                          {"summary": "业务结论暂未生成，保留查询结果与基本说明",
                           "error_type": type(exc).__name__}, "result_summarizer")
    if revision(run) != initial or run.agent_memory.get("user_cancelled"):
        raise TaskSupersededError("需求或结果已更新，丢弃过期结果说明")
    workflow.emit(run, "result.summary.completed", RunStatus.EXPLAINING,
                  {"summary": text, "mode": mode}, "result_summarizer")
    return text
