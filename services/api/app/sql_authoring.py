"""Understand once, clarify in the existing form, then author SQL directly."""

from copy import deepcopy
import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model
from semora_agent.model import ModelError, TaskSupersededError
from .query_models import AnalysisOutcome
from semora_agent.runtime import AgentSubmission, run_agent
from semora_agent.sql_prompts import UNDERSTAND, AUTHOR, REPAIR, COVERAGE
from semora_contracts import (
    SQLProgram,
    QueryPlan,
    RequirementDraft,
    QueryConfigurationGap,
    QueryUnderstanding,
)


class Understanding(QueryUnderstanding):
    """Model boundary shares the persisted understanding contract."""

    model_config = ConfigDict(extra="forbid")
    target_state: Literal["known", "needs_direction"] = "known"
    technical_gaps: list[QueryConfigurationGap] = Field(default_factory=list, max_length=16)


class StepRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=100000, description="修复后的完整SQL，使用当前步骤指定方言，参数使用:name。")
    parameters: dict = Field(default_factory=dict)
    change_summary: str = Field(
        min_length=1,
        max_length=500,
        description="执行记录：简短说明改了什么、为什么改，可使用表列名、函数名和SQL表达式；不声称尚未完成的校验或执行已成功。",
    )


class CoverageEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: str
    sql: str = Field(min_length=1, max_length=100000, description="业务冲突修正后的完整SQL，使用该步骤指定方言，参数使用:name。")
    parameters: dict | None = None
    requirement_quote: str = Field(min_length=1, max_length=500)
    change_summary: str = Field(
        min_length=1,
        max_length=500,
        description="用业务语言说明原先和现在的含义差别，不出现表列名或SQL；具体修改放sql。",
    )


class CoverageReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edits: list[CoverageEdit] = Field(default_factory=list, max_length=4)
    summary: str = Field(
        default="",
        max_length=1200,
        description="修正后最终查询的业务说明，不是修复过程。不含字段标识、SQL或技术实现，不编造未执行的数据结论。",
    )


def apply_coverage_edits(program, review):
    candidate = program.model_copy(deep=True)
    seen = set()
    for edit in review.edits:
        step = next((s for s in candidate.steps if s.name == edit.step), None)
        if step is None or edit.step in seen:
            raise ValueError("修正必须指向已有且不重复的步骤name")
        seen.add(edit.step)
        step.sql = edit.sql
        if edit.parameters is not None:
            step.parameters = edit.parameters
        step.purpose = edit.change_summary
    if review.edits and review.summary:
        candidate.summary = review.summary
    return candidate


def material(run):
    """One copy of definitions, without the retired compiler's instructions."""
    from .model_context import project_knowledge

    packet = deepcopy((run.agent_memory.get("prepared_context") or [{}])[-1].get("packet", {}))
    # Physical scan comments duplicate published descriptions. Keep types and
    # nullability, including information not yet represented in the ontology.
    for schema in packet.get("schemas", []):
        obj = next(
            (
                o["object"]
                for o in packet.get("objects", [])
                if o["object"]["name"] == schema["object"]
            ),
            {},
        )
        columns = {p.get("column", p["name"]) for p in obj.get("properties", [])}
        for table in schema.get("tables", []):
            table["columns"] = [
                {k: c[k] for k in ("name", "type", "nullable", "primary_key") if k in c}
                for c in table.get("columns", [])
                if c.get("name") in columns
            ]
    return project_knowledge(packet, run)


def context(workflow, run, question):
    from .prepared_context import confirmed_fact_context
    from .datasources import datasource_service

    packet = material(run)
    ids = {o["object"].get("dataSourceId") for o in packet.get("objects", [])}
    sources = []
    for source_id in ids - {None, ""}:
        source = datasource_service.repository.get(source_id)
        from .database_profile import source_profile

        from .sql_dialect import dialect_for
        sources.append({"id": source_id, "name": source.name, **source_profile(source), "dialect": dialect_for(source_id)})
    return {
        "sql_contract": {"authoring_dialect": "per_step", "local_dialect": "duckdb", "parameter_style": ":name"},
        "request": question,
        "confirmed_answers": confirmed_fact_context(workflow, run),
        "materials": packet,
        "data_sources": sources,
        "current_time": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "timezone": "Asia/Shanghai",
    }


def sql_context(workflow, run, question, requirement):
    """Give authoring a complete business contract, with user facts kept distinct."""
    payload = context(workflow, run, question)
    value = requirement.model_dump(mode="json") if hasattr(requirement, "model_dump") else dict(requirement or {})
    payload["request"] = {
        key: value[key]
        for key in value
        if key not in {"evidence", "confidence", "clarifications", "technical_gaps", "status",
                       "clarification_stage", "clarification_round", "confirmed_facts",
                       "confirmed_by", "confirmed_at", "content_hash"}
    }
    payload["original_request"] = question
    return payload


MAX_SQL_REPAIRS = 3


def reserve_sql_repair(run, issue):
    count = int((run.agent_memory.get("sql_repair_count") or [{}])[-1].get("attempts", 0))
    if count >= MAX_SQL_REPAIRS:
        raise ValueError(f"SQL 经{MAX_SQL_REPAIRS}轮定向修复仍未通过：" + str(issue))
    run.agent_memory["sql_repair_count"] = [{"attempts": count + 1}]
    return count + 1


async def structured(workflow, run, prompt, payload, schema):
    """A malformed response gets one format correction, never a fresh ReAct."""
    from .run_state import revision
    from .sql_model_context import sql_model_payload
    from .sql_execution_scope import program_output_contract
    from .sql_input_compaction import compact_output_schema

    initial = revision(run)
    value = None
    contract_error = ""
    format_corrections = 0
    from .sql_errors import SQLIssue
    for attempt in range(MAX_SQL_REPAIRS + 2):
        previous_value = value
        if attempt:
            reserve_sql_repair(run, contract_error)
        try:
            raw = await workflow.model.complete_json(
                prompt,
                json.dumps(
                    {
                        **sql_model_payload(payload, reference_materials=material(run)),
                        "output_contract": compact_output_schema(program_output_contract(payload, schema.model_json_schema())
                        if schema is SQLProgram else schema.model_json_schema()),
                        **(
                            {
                                "invalid_response": value,
                                "contract_error": contract_error,
                                "correction_required": "前次响应不可执行。按错误修正完整结构，不得原样重复提交。",
                            }
                            if attempt
                            else {}
                        ),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
            if revision(run) != initial:
                raise TaskSupersededError("需求已更新，丢弃过期模型响应")
            value = raw
            parsed = schema.model_validate(raw)
            candidate = parsed if schema is SQLProgram else None
            if schema is StepRepair:
                candidate = SQLProgram.model_validate(payload["program"])
                step = next(s for s in candidate.steps if s.name == payload["current_step"])
                step.sql, step.parameters = parsed.sql, parsed.parameters
            if schema is CoverageReview and parsed.edits:
                original = SQLProgram(
                    summary="SQL核对",
                    steps=[{**s, "purpose": "原步骤"} for s in payload["program_steps"]],
                )
                candidate = apply_coverage_edits(original, parsed)
            if candidate is not None:
                allowed = {s["id"] for s in payload.get("data_sources", [])}
                if any(
                    s.engine == "source" and s.data_source_id not in allowed
                    for s in candidate.steps
                ):
                    raise ValueError("source步骤必须使用资料中给定的完整data_source_id")
                from .sql_dialect import readonly_tree, validate_grouping

                previous = []
                objects = [o["object"] for o in payload.get("materials", {}).get("objects", [])]
                for step in candidate.steps:
                    if schema is StepRepair and step.name != payload["current_step"]:
                        previous.append(step.name)
                        continue
                    tables = (
                        previous
                        if step.engine == "local"
                        else [
                            o["table"]
                            for o in objects
                            if o.get("dataSourceId") == step.data_source_id
                        ]
                    )
                    try:
                        from .sql_dialect import step_dialect
                        step.dialect = step_dialect(step)
                        tree = readonly_tree(step.sql, tables, step.dialect)
                        if step.engine == "source":
                            from .sql_column_refs import validate_physical_column_refs

                            validate_physical_column_refs(tree, [o for o in objects
                                if o.get("dataSourceId") == step.data_source_id])
                        validate_grouping(tree, objects)
                        if step.engine == "source":
                            from .database_profile import validate_features

                            profile = next(
                                s for s in payload["data_sources"] if s["id"] == step.data_source_id
                            )
                            validate_features(tree, profile)
                    except ValueError as exc:
                        raise SQLIssue(
                            f"步骤{step.name}的{step.engine}查询校验未通过：{exc}。"
                            f"本步骤可用表：{tables}。保持确认目标，针对错误修正完整SQLProgram；"
                            "同源关联直接在source SQL中完成，local不能访问物理表。"
                        ) from exc
                    previous.append(step.name)
            if candidate is not None and schema is SQLProgram:
                parsed = candidate
            return parsed
        except TaskSupersededError:
            raise
        except (ValueError, ModelError) as exc:
            # Transport timeouts/auth failures are not malformed JSON.
            if isinstance(exc, ModelError) and str(exc) not in {
                "模型返回的 JSON 无法解析",
                "模型返回必须是 JSON 对象",
                "模型未返回 JSON 文本",
            }:
                raise
            if isinstance(exc, SQLIssue):
                if exc.kind != "sql":
                    raise
                diagnostic = {"attempt": attempt + 1, "error_stage": exc.stage,
                              "error_type": exc.kind, "reason": str(exc)[:1600]}
                run.agent_memory.setdefault("sql_validation_rejections", []).append(diagnostic)
                run.agent_memory["sql_validation_rejections"] = run.agent_memory["sql_validation_rejections"][-8:]
                if callable(getattr(workflow, "emit", None)):
                    workflow.emit(run, "sql.validation.rejected", run.status,
                                  {**diagnostic, "summary": "SQL未通过程序校验：" + str(exc)[:1600]}, "sql_author")
                if previous_value is not None and value == previous_value:
                    raise ValueError("模型重复提交相同无效SQL，已停止重复修复；校验原因：" + str(exc)[:1600]) from exc
            elif format_corrections:
                raise
            else:
                format_corrections += 1
            contract_error = str(exc)[:1600]


async def analyse(workflow, run, question, ontology, scope):
    from .prepared_context import prepare_context
    from .clarifications import validate_questions
    from .clarifications import (
        clarification_policy,
        prune_answered_questions,
        route_business_definitions,
    )
    from .run_state import revision

    initial = revision(run)
    # Reuse and supplementary tools must agree on the authoritative revision.
    from .prepared_context import confirmed_fact_context

    confirmed_fact_context(workflow, run)
    saved = await prepare_context(workflow, run, question, ontology, scope)
    # Root dataSourceId is inherited by objects in single-source domains.
    for obj in saved["packet"].get("objects", []):
        obj["object"].setdefault("dataSourceId", ontology.get("dataSourceId", ""))
    if revision(run) != initial:
        raise TaskSupersededError("资料准备期间需求已更新")
    policy = clarification_policy(run)
    previous = run.agent_memory.get("sql_understanding", [])
    if not policy["business_allowed"]:
        legacy = run.query_rehearsal or (run.continuation or {}).get("query_rehearsal")
        legacy = legacy.model_dump(mode="json") if hasattr(legacy, "model_dump") else legacy or {}
        snapshot = (
            previous[-1]
            if previous
            else {k: v for k, v in legacy.items() if k in Understanding.model_fields}
        )
        understood = Understanding.model_validate(snapshot or {"summary": question})
        understood.questions = []
        understood.technical_gaps = [
            g
            for g in understood.technical_gaps
            if all(
                any(
                    f.question_id == c.question_id and f.value in c.values for f in run.user_answers
                )
                for c in g.depends_on
            )
        ]
        for gap in understood.technical_gaps:
            gap.depends_on = []
    else:
        from .research_tools import ResearchTools

        toolkit = ResearchTools(workflow, run, ontology, scope)
        toolkit.start_phase("需求理解与口径确认")
        toolkit.context_state = saved

        def guard():
            if revision(run) != initial:
                raise TaskSupersededError("需求已更新，停止旧分析")

        toolkit.task_guard = guard
        task = context(workflow, run, question)
        task["clarification_policy"] = policy
        from .prepared_context import lightweight_object_catalog
        available_catalog = {obj["name"]: obj for obj in ontology.get("objects", [])}
        bounded_catalog = lightweight_object_catalog(
            available_catalog,
            ontology.get("relations", []),
            question + " " + " ".join(str(item.get("value", "")) for item in confirmed_fact_context(workflow, run)),
        )
        task["object_catalog"] = [
            {k: o.get(k) for k in ("name", "label", "description")}
            for o in bounded_catalog.values()
        ]
        task["knowledge_base_ids"] = ontology.get("knowledgeBaseIds", [])
        submission = create_model(
            "AgentSubmission", __base__=AgentSubmission, payload=(Understanding, ...)
        )

        def parse(value):
            report = Understanding.model_validate(value)
            # Reuse form dependency validation, not the query compiler.
            validate_questions(report.questions)
            from .query_alignment import validate_required_object_gaps

            errors = validate_required_object_gaps(report, toolkit.context_state["packet"])
            if errors:
                raise ValueError("；".join(errors) + "。重新核对最小必要对象；移除可省略关联的缺口，保留有依据的真实障碍。")
            return report

        from .query_context_wire import encode_payload

        tools = toolkit.tools()
        value, memory = await run_agent(
            workflow.model,
            UNDERSTAND,
            encode_payload(task),
            tools,
            [],
            toolkit.event,
            parse,
            submission,
            max_models=12,
            max_tools=10,
            task_guard=guard,
            bounded_protocol=True,
            track_progress=False,
        )
        understood = parse(value)
        run.agent_memory["analyst"] = memory
    report = QueryUnderstanding(**understood.model_dump(mode="json"))
    if policy["business_allowed"] and not run.user_answers:
        from .field_value_probe import numeric_grouping_question

        grouping = numeric_grouping_question(question, report, saved["packet"])
        if grouping:
            report.questions.append(grouping)
    from .query_relation_repair import supplement
    await supplement(workflow, run, question, ontology, scope, saved, report)
    route_business_definitions(report)
    prune_answered_questions(report, run.user_answers)
    validate_questions(report.questions)
    from .business_copy import polish_public_text

    await polish_public_text(workflow, run, report)
    from .query_alignment import validate_logical_refs
    logical_plan_errors = validate_logical_refs(report.logical_plan, saved["packet"])
    if logical_plan_errors:
        # The internal plan is an aid, never a new failure exit for a valid user request.
        run.agent_memory["logical_plan_validation"] = [{"errors": logical_plan_errors}]
        report.logical_plan = None
    if (
        report.questions
        and not policy["business_allowed"]
        and report.target_state != "needs_direction"
    ):
        raise ValueError("已提交业务答案，生成过程不能再次新增同阶段问题")
    run.query_rehearsal = report
    run.agent_memory["sql_understanding"] = [report.model_dump(mode="json")]
    draft = RequirementDraft(
        summary=report.summary,
        analysis_steps=report.skeleton,
        **report.requirements.model_dump(mode="json"),
        evidence=run.evidence,
        clarifications=report.questions,
        technical_gaps=report.technical_gaps,
        clarification_stage="direction" if report.target_state == "needs_direction" else "business",
        clarification_round=policy["answered_rounds"],
        status="awaiting_clarification" if report.questions or report.technical_gaps else "ready",
        logical_plan=report.logical_plan,
    )
    workflow.emit(
        run,
        "rehearsal.ready",
        run.status,
        {
            "summary": report.summary,
            "questions": len(report.questions),
            "technical_gaps": len(report.technical_gaps),
            "skeleton": report.skeleton,
        },
        "analyst",
    )
    if report.questions or report.technical_gaps:
        return AnalysisOutcome(plan=None, requirement=draft, model_mode="sql-native")
    payload = sql_context(workflow, run, question, draft)
    run.agent_memory["sql_request_context"] = [{
        "request": question,
        "requirement_contract": deepcopy(payload["request"]),
    }]
    program = await structured(workflow, run, AUTHOR, payload, SQLProgram)
    program = await check_coverage(workflow, run, payload, program)
    await polish_public_text(workflow, run, program)
    plan = QueryPlan(
        domain=run.domain,
        intent=program.summary,
        sql_program=program,
        execution_mode="sequential_queries" if len(program.steps) > 1 else "single_query",
        assumptions=program.assumptions,
        evidence=run.evidence,
        logical_plan=draft.logical_plan,
    )
    return AnalysisOutcome(plan=plan, requirement=draft, model_mode="sql-native")


async def check_coverage(workflow, run, payload, program):
    """One independent comparison, no tools, no review/author negotiation loop."""
    from sqlglot import exp
    import sqlglot

    from .query_alignment import inspect_alignment
    logical = payload.get("request", {}).get("logical_plan")
    from semora_contracts import LogicalQueryPlan
    logical = LogicalQueryPlan.model_validate(logical) if logical else None
    alignment = inspect_alignment(logical, program, payload.get("materials", {}))
    run.agent_memory["sql_alignment"] = [{"findings": alignment}]
    # A plain projection with no deterministic finding has no scope/aggregation interaction to reconcile.
    from .sql_dialect import parse_query
    trees = [parse_query(s.sql, s.dialect or "postgres") for s in program.steps]
    if not alignment and len(trees) == 1 and not any(trees[0].find(t) for t in (exp.AggFunc, exp.Join, exp.Subquery)):
        return program
    try:
        packet = payload.get("materials", {})
        comparison = {
            "sql_contract": payload["sql_contract"],
            "request": payload.get("original_request", payload["request"]),
            "logical_plan": logical.model_dump(mode="json") if logical else None,
            "confirmed_answers": payload.get("confirmed_answers", []),
            "data_sources": payload.get("data_sources", []),
            "materials": {
                "objects": [
                    {
                        "object": {
                            **{
                                k: o["object"].get(k)
                                for k in (
                                    "name",
                                    "table",
                                    "primaryKey",
                                    "dataSourceId",
                                    "projection",
                                )
                            },
                            "properties": [
                                {k: p.get(k) for k in ("name", "column", "label", "nullable")}
                                for p in o["object"].get("properties", [])
                            ],
                        },
                    }
                    for o in packet.get("objects", [])
                ],
                "relations": packet.get("relations", []),
                "identity_catalog": packet.get("identity_catalog", []),
            },
            "program_steps": [
                s.model_dump(mode="json", exclude={"purpose"}) for s in program.steps
            ],
            "deterministic_alignment_findings": alignment,
        }
        reviewed = await asyncio.wait_for(
            structured(
                workflow,
                run,
                COVERAGE,
                comparison,
                CoverageReview,
            ),
            timeout=60,
        )
    except TaskSupersededError:
        raise
    except (ModelError, ValueError, TimeoutError) as exc:
        workflow.emit(
            run,
            "sql.coverage.unavailable",
            run.status,
            {
                "summary": "一致性核对未完成，保留原SQL进入数据库校验",
                "error_type": type(exc).__name__,
                "reason": str(exc)[:400],
            },
            "sql_author",
        )
        run.delivery_warnings.append("SQL已生成，一致性复查未完成；后续继续数据库校验与只读执行。")
        return program
    if reviewed.edits:
        workflow.emit(
            run,
            "sql.semantic.corrected",
            run.status,
            {"summary": "；".join(e.change_summary for e in reviewed.edits)[:1200]},
            "sql_author",
        )
        candidate = apply_coverage_edits(program, reviewed)
        run.agent_memory["sql_alignment"] = [{
            "findings": inspect_alignment(logical, candidate, payload.get("materials", {})),
            "after_coverage": True,
        }]
        return candidate
    return program


async def repair_step(workflow, run, name, issue):
    attempt = reserve_sql_repair(run, issue)
    step = next(s for s in run.plan.sql_program.steps if s.name == name)
    saved_request = (run.agent_memory.get("sql_request_context") or [{}])[-1]
    question = saved_request.get("request", run.question)
    requirement = saved_request.get("requirement_contract") or run.requirement or run.requirement_draft
    payload = sql_context(workflow, run, question, requirement)
    artifact = run.source_artifacts.get(name)
    # A compile failure may leave the previous artifact in memory. Never
    # present that stale target statement as the output of the current source.
    current_artifact = artifact if artifact and artifact.semantic_sql == step.sql else None
    from .sql_dialect import dialect_for

    payload.update(
        confirmed_requirement=run.requirement.model_dump(mode="json") if run.requirement else None,
        program=run.plan.sql_program.model_dump(mode="json"),
        current_step=name,
        editable_baseline={"dialect": "duckdb" if step.engine == "local" else dialect_for(step.data_source_id), "sql": step.sql, "parameters": step.parameters},
        execution_diagnostic={
            "target_dialect": "duckdb" if step.engine == "local" else dialect_for(step.data_source_id),
            "target_sql": current_artifact.physical_sql if current_artifact else None,
            "error": issue,
        },
        available_results={
            key: {"columns": r.columns, "column_types": r.column_types}
            for key, r in run.source_results.items()
        },
        prior_repairs=run.agent_memory.get("sql_repairs", []),
    )
    fixed = await structured(workflow, run, REPAIR, payload, StepRepair)
    if fixed.sql.strip() == step.sql.strip() and fixed.parameters == step.parameters:
        raise ValueError("修复未改变产生错误的SQL，已停止重复处理")
    from .run_state import digest

    before_hash = digest([step.sql, step.parameters])
    from .sql_dialect import step_dialect
    step.sql, step.parameters, step.dialect = fixed.sql, fixed.parameters, step_dialect(step)
    run.agent_memory.setdefault("sql_repairs", []).append(
        {"step": name, "error": issue, "change_summary": fixed.change_summary, "attempt": attempt}
    )
    workflow.emit(
        run,
        "sql.repaired",
        run.status,
        {"step": name, "summary": fixed.change_summary, "attempt": attempt,
         "error_stage": issue.get("error_stage"), "error_type": issue.get("error_type"),
         "baseline_before_hash": before_hash, "baseline_after_hash": digest([step.sql, step.parameters])},
        "sql_author",
    )
