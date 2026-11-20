from __future__ import annotations
import asyncio
import hashlib
import copy
import json
import os
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable
import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from semora_agent.graph import AgentState, build_sql_generation_graph
from semora_agent.model import TaskSupersededError, structured_model
from semora_contracts import (
    AuthorizationContext,
    ExecutionResult,
    OntologySnapshotRef,
    QueryRun,
    RunEvent,
    RunStatus,
    SystemSettings,
)
from semora_telemetry import traced_operation
from .ontology import ontology_service
from .continuation import handoff
from .evidence_policy import EVIDENCE_REUSE_TTL_SECONDS
from .store import Store
from .settings import settings_service
from .executor_client import ExecutorClient

Executor = Callable[[str, dict, list[str], dict], Awaitable[ExecutionResult]]
InjectedExecutor = Callable[[str, dict, list[str]], Awaitable[ExecutionResult]]
Estimator = Callable[[str, dict, str], Awaitable[dict]]
SnapshotLoader = Callable[[str], Awaitable[tuple[OntologySnapshotRef, dict]]]


class Workflow:
    def __init__(
        self,
        store: Store,
        execute: InjectedExecutor | None = None,
        snapshot_loader: SnapshotLoader | None = None,
        checkpointer=None,
        estimate: Estimator | None = None,
        model=None,
    ) -> None:
        self.store = store
        executor_client = ExecutorClient()
        if execute is None:
            self.execute: Executor = executor_client.execute
        else:

            async def execute_adapter(
                sql: str, parameters: dict, sensitive_columns: list[str], _: dict
            ) -> ExecutionResult:
                return await execute(sql, parameters, sensitive_columns)

            self.execute = execute_adapter
        if estimate is not None or execute is not None:
            injected_estimate = estimate or executor_client.static_estimate

            async def estimate_adapter(sql, parameters, data_source_id, _timeout_seconds):
                return await injected_estimate(sql, parameters, data_source_id)

            self.estimate = estimate_adapter
        else:
            self.estimate = executor_client.estimate
        self.snapshot_loader = snapshot_loader or ontology_service.latest_snapshot_bundle
        self.model = model or structured_model
        self._tasks: dict[str, asyncio.Task] = {}
        self._checkpointer = checkpointer or InMemorySaver()
        self._build_graph()

    def _build_graph(self) -> None:
        self.graph = build_sql_generation_graph(
            retrieve=self._retrieve_node,
            analyse=self._analyse_node,
            align=self._align_node,
            compile_sql=self._compile_node,
            validate_sql=self._validate_sql_node,
            deliver=self._deliver_sql_node,
            route_after_step=self._route_after_step,
            checkpointer=self._checkpointer,
            execute_dependencies=self._execute_dependencies_node,
        )

    def configure_checkpointer(self, checkpointer) -> None:
        self._checkpointer = checkpointer
        self._build_graph()

    def runtime_info(self) -> dict[str, str]:
        return {
            "engine": "langgraph",
            "agents": "langchain.create_agent / tool-calling",
            "checkpointer": type(self._checkpointer).__name__,
            "purpose": "query_understanding_confirmation_execution",
        }

    async def _ontology_snapshot(self, domain: str) -> tuple[OntologySnapshotRef, dict]:
        try:
            reference, ontology = await self.snapshot_loader(domain)
        except Exception as exc:
            from .ontology import OntologyCoreError

            if not isinstance(exc, OntologyCoreError) or exc.status_code != 404:
                raise
            ontology = await ontology_service.get_draft(domain)
            digest = hashlib.sha256(
                json.dumps(ontology, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            reference = OntologySnapshotRef(
                domain=domain,
                version=ontology["version"],
                content_hash="sha256:" + digest,
                artifact_version="draft",
                data_source_id=ontology.get("dataSourceId", ""),
            )
        if reference.domain != domain or ontology.get("domain") != domain:
            raise ValueError("本体快照业务域与会话不一致")
        return reference, ontology

    def emit(
        self,
        run: QueryRun,
        event: str,
        status: RunStatus,
        data: dict | None = None,
        actor: str = "conversation_owner",
    ) -> None:
        run.status = status
        from .delivery import build_delivery

        run.delivery = build_delivery(run)
        if status == RunStatus.FAILED and not run.answer and run.delivery:
            run.answer = run.delivery.summary
        payload = data or {}
        from semora_agent.model_diagnostics import debug_payload

        if not event.startswith("agent.request."):
            debug_payload(
                "workflow.event",
                {"name": event, "status": status.value, "actor": actor, "payload": payload},
            )
        run.events.append(RunEvent(event=event, status=status, data=payload))
        self.store.save_run(run)

    def _run(self, state: AgentState) -> QueryRun:
        run_id = state["run_id"]
        if run_id not in self.store.runs:
            raise RuntimeError(f"run state not found: {run_id}")
        return self.store.runs[run_id]

    def _route_after_step(self, state: AgentState) -> str:
        if state.get("task_superseded"):
            return "end"
        status = self._run(state).status
        return (
            "end"
            if status in {RunStatus.FAILED, RunStatus.CLARIFYING, RunStatus.COMPLETED}
            else "continue"
        )

    def _discard_superseded_task(self, run):
        self.emit(
            run,
            "task.result.superseded",
            run.status,
            {"summary": "输入或权限已更新，旧任务结果未写入当前方案，也未继续执行"},
            "conversation_owner",
        )
        return {"task_superseded": True}

    async def _retrieve_node(self, state: AgentState) -> dict:
        run = self._run(state)
        self.emit(run, "retrieval.started", RunStatus.RETRIEVING)
        try:
            run.ontology, ontology_model = await self._ontology_snapshot(run.domain)
            source_id = str(ontology_model.get("dataSourceId") or "")
            if not source_id:
                raise ValueError("本体未绑定数据源")
            if run.ontology.data_source_id and run.ontology.data_source_id != source_id:
                raise ValueError("本体快照数据源绑定不一致")
            if run.continuation:
                source = self.store.runs.get(run.continuation["source_run_id"])
                same = run.continuation.get("ontology") == run.ontology.model_dump(mode="json")
                run.continuation["bindings_changed"] = not same
                origin = datetime.fromisoformat(run.continuation["evidence_origin_at"])
                if (
                    source
                    and same
                    and (datetime.now(timezone.utc) - origin).total_seconds()
                    <= EVIDENCE_REUSE_TTL_SECONDS
                ):
                    allowed = set(ontology_model.get("knowledgeBaseIds") or [])
                    run.evidence = [
                        e.model_copy(deep=True)
                        for e in source.evidence
                        if e.knowledge_base_id in allowed
                    ]
                if not run.evidence:
                    run.continuation["evidence_origin_at"] = run.created_at.isoformat()
                self.emit(
                    run,
                    "conversation.context.restored",
                    RunStatus.UNDERSTANDING,
                    {
                        "summary": "已接续需求与确认口径",
                        "source_run_id": run.continuation["source_run_id"],
                        "reused_evidence": len(run.evidence),
                        "bindings_changed": not same,
                    },
                )
            self.emit(
                run,
                "ontology.bound",
                RunStatus.UNDERSTANDING,
                {
                    "domain": run.domain,
                    "version": run.ontology.version,
                    "data_source_id": source_id,
                },
                "ontology",
            )
            return {"ontology_model": ontology_model}
        except Exception as exc:
            run.error = f"业务域绑定不可用：{exc}"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "ontology")
            return {}

    def _analysis_question(self, run: QueryRun) -> str:
        if run.message_mode == "new":
            return run.question
        if run.continuation:
            c = run.continuation
            return json.dumps(
                {
                    "需求单": c.get("requirement_form", {}),
                    "原需求及后续补充": c["question_history"],
                    "此前确认上下文": c.get("prior_confirmed_context", []),
                    "已确认口径": c["confirmed_facts"],
                    "历史补充确认": c.get("legacy_confirmations", ""),
                    "本轮补充": run.question,
                },
                ensure_ascii=False,
            )
        session = self.store.sessions.get(run.session_id)
        form = session.requirement_form if session else {}
        history = self._confirmed_history(run)
        if not form and not history:
            return run.question
        return json.dumps(
            {"需求单": form, "已确认上下文": history[-3:], "本轮问题": run.question},
            ensure_ascii=False,
        )

    def _confirmed_history(self, run):
        session = self.store.sessions.get(run.session_id)
        history = []
        if session:
            for run_id in session.run_ids:
                previous = self.store.runs.get(run_id)
                if previous and previous.id != run.id and previous.message_mode == "new":
                    history = []
                if (
                    previous
                    and previous.id != run.id
                    and previous.requirement
                    and previous.domain == run.domain
                    and previous.authorization == run.authorization
                ):
                    history.append(
                        {
                            "question": previous.question,
                            "confirmed_requirement": previous.requirement.model_dump(mode="json"),
                        }
                    )
        return history

    async def _analyse_node(self, state: AgentState) -> dict:
        run = self._run(state)
        self.emit(run, "understanding.started", RunStatus.UNDERSTANDING)
        ontology_model = state.get("ontology_model")
        if not ontology_model or run.ontology is None:
            run.error = "会话本体快照缺失，已安全停止分析"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "ontology")
            return {}
        try:
            scope = {}
            outcome = await self._understand_and_author(
                run, self._analysis_question(run), ontology_model, scope
            )
        except Exception as exc:
            if isinstance(exc, TaskSupersededError):
                return self._discard_superseded_task(run)
            label = "需求分析失败"
            run.error = f"{label}：{getattr(exc, 'public_message', str(exc))}"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "analyst")
            return {}
        if outcome.requirement is None:
            run.error = "Analyst 未返回结构化需求，已安全停止分析"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "analyst")
            return {}
        run.requirement_draft = outcome.requirement
        run.proposed_plan = outcome.plan
        self.emit(
            run,
            "requirement.draft.ready",
            RunStatus.ALIGNING,
            {
                "requirement": run.requirement_draft.model_dump(mode="json"),
                "analyst_mode": outcome.model_mode,
            },
            "analyst",
        )
        return {
            "ontology_model": ontology_model,
            "alignment_initial_draft": run.requirement_draft.model_dump(mode="json"),
        }

    async def _understand_and_author(self, run, question, ontology, scope):
        from .sql_authoring import analyse

        self.emit(
            run,
            "analysis.started",
            RunStatus.UNDERSTANDING,
            {"summary": "正在结合当前需求与补充信息分析"},
            "analyst",
        )
        return await analyse(self, run, question, ontology, scope)

    async def _align_node(self, state: AgentState) -> dict:
        from .requirement_alignment import RequirementAlignment

        return await RequirementAlignment(self).run(state)

    async def _compile_node(self, state: AgentState) -> dict:
        from .sql_execution import SQLExecution

        run = self._run(state)
        if run.plan is None or run.plan.sql_program is None:
            run.error = "查询执行步骤缺失，请重新生成当前需求的SQL"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error})
            return {}
        self.emit(run, "compilation.started", RunStatus.COMPILING)
        try:
            if run.ontology and run.ontology.artifact_version == "draft":
                raise ValueError("当前本体仍为草稿，请先发布本体后重新生成SQL")
            await SQLExecution(self, run, state["ontology_model"], state["settings"]).compile()
        except TaskSupersededError:
            return self._discard_superseded_task(run)
        except Exception as exc:
            run.error = f"SQL生成未完成：{exc}"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "sql_author")
        return {}

    async def _validate_sql_node(self, state: AgentState) -> dict:
        from .sql_execution import SQLExecution
        from semora_agent.model import TaskSupersededError

        run = self._run(state)
        try:
            await SQLExecution(self, run, state["ontology_model"], state["settings"]).validate()
        except TaskSupersededError:
            return self._discard_superseded_task(run)
        except Exception as exc:
            from .executor_errors import ExecutorOperationError

            if isinstance(exc, ExecutorOperationError):
                run.agent_memory["execution_failure"] = [exc.public()]
            run.error = f"SQL校验未完成：{exc}"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "executor")
        return {}

    async def _deliver_sql_node(self, state: AgentState) -> dict:
        run = self._run(state)
        run.error = None
        if run.result is not None:
            from .result_summary import summarize_result

            try:
                run.answer = await summarize_result(self, run)
            except TaskSupersededError:
                return self._discard_superseded_task(run)
            self.emit(
                run,
                "answer.completed",
                RunStatus.COMPLETED,
                {"answer": run.answer, "delivery_kind": "result"},
            )
            return {}
        run.error = "SQL 已生成，但尚未取得执行结果；已保留查询方案和 SQL。"
        self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error})
        return {}

    async def _execute_dependencies_node(self, state: AgentState) -> dict:
        from .sql_execution import SQLExecution

        run = self._run(state)
        try:
            await SQLExecution(self, run, state["ontology_model"], state["settings"]).execute()
        except TaskSupersededError:
            return self._discard_superseded_task(run)
        except Exception as exc:
            from .executor_errors import ExecutorOperationError

            if isinstance(exc, ExecutorOperationError) and not run.agent_memory.get('execution_failure'):
                run.agent_memory["execution_failure"] = [exc.public()]
            run.result = None
            failure = (run.agent_memory.get('execution_failure') or [{}])[-1]
            run.error = ('查询失败，' + failure['message'] if failure.get('error_type') == 'timeout'
                         else f"查询执行未完成：{exc}")
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "executor")
        return {}

    def _config(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 30}

    def _prepare_run(
        self,
        session_id: str,
        question: str,
        domain: str | None,
        authorization: AuthorizationContext | None,
        mode: str = "auto",
    ) -> tuple[QueryRun, SystemSettings]:
        authorization = authorization or AuthorizationContext(
            subject="test-user",
            username="测试分析员",
            roles=["analyst"],
            branch_ids=[1, 2, 3],
            sensitivity_clearance="confidential",
        )
        if mode not in {"auto", "continue", "new"}:
            raise ValueError("未知消息模式")
        if mode == "auto" and re.match(
            r"^\s*(?:新需求|新的需求|换个需求|换一个需求|另一个需求|另起一个需求)(?:[：:，,\s]|$)",
            question,
        ):
            mode = "new"
        session = self.store.sessions[session_id]
        domain = session.domain if domain is None else domain
        if not domain or not re.fullmatch(r"[a-z][a-z0-9_]{1,99}", domain):
            raise ValueError("请先选择业务域，再提交查询需求")
        if session.domain and session.domain != domain:
            raise ValueError("当前需求已绑定其他业务域，请新建需求后再查询")
        if not session.domain:
            session.domain = domain
            self.store._persist_session(session)
        run = QueryRun(
            session_id=session_id,
            domain=domain,
            question=question,
            authorization=authorization,
            message_mode=mode,
        )
        source = self.store.runs.get(session.run_ids[-1]) if session.run_ids else None
        unchanged_retry = question.strip().rstrip("。！!") in {
            "重试",
            "继续",
            "再试一次",
            "重新执行",
            "按原口径继续",
        }
        # Recover a legacy retry that was incorrectly started as a fresh attempt.
        # Never cross an explicit new-demand boundary or authorization change.
        if (
            mode != "new"
            and unchanged_retry
            and source
            and source.message_mode != "new"
            and not source.continuation
            and not source.user_answers
            and source.question.strip().rstrip("。！!")
            in {"重试", "继续", "再试一次", "重新执行", "按原口径继续"}
        ):
            for previous_id in reversed(session.run_ids[:-1]):
                previous = self.store.runs[previous_id]
                if previous.domain != domain or previous.authorization != authorization:
                    break
                if previous.requirement is not None and previous.plan is not None:
                    source = previous
                    break
                if previous.message_mode == "new":
                    break
        if (
            mode != "new"
            and source
            and (
                source.status in {RunStatus.FAILED, RunStatus.CLARIFYING}
                or (
                    source.status == RunStatus.COMPLETED and (mode == "continue" or unchanged_retry)
                )
            )
        ):
            if source.domain == domain and source.authorization == authorization:
                run.continuation = handoff(source, session)
                prior = self._confirmed_history(source) if source.message_mode != "new" else []
                if prior:
                    run.continuation["prior_confirmed_context"] = prior[-3:]
                    if not run.continuation.get("confirmed_requirement"):
                        run.continuation["confirmed_requirement"] = prior[-1][
                            "confirmed_requirement"
                        ]
                run.user_answers = copy.deepcopy(source.user_answers)
            else:
                # Changed permissions must never inherit old field/evidence data.
                run.message_mode = "new"
        settings = settings_service.get()
        self.store.add_run(run)
        self.emit(
            run,
            "run.started",
            RunStatus.RECEIVED,
            {
                "question": question,
                "domain": domain,
                "authorization": authorization.model_dump(mode="json"),
            },
            authorization.username,
        )
        if run.continuation:
            self.emit(
                run,
                "conversation.continued",
                RunStatus.RECEIVED,
                {
                    "source_run_id": source.id,
                    "summary": "继续上次失败的需求分析，保留已确认口径，重新生成 SQL",
                },
                "conversation_owner",
            )
        return run, settings

    async def _invoke_graph(self, run, inputs, *, config):
        from semora_agent.model_diagnostics import bind_run_log, debug_payload

        counter = run.agent_memory.setdefault("http_log_state", [{}])[-1]
        with bind_run_log(run, counter):
            debug_payload(
                "workflow.enter",
                {"question": run.question, "resume": getattr(inputs, "resume", None)},
            )
            try:
                return await self.graph.ainvoke(inputs, config=config)
            finally:
                debug_payload("workflow.leave", {"status": run.status.value, "error": run.error})

    async def _invoke(
        self, run: QueryRun, settings: SystemSettings, *, auto_align: bool = False
    ) -> QueryRun:
        run.agent_memory["runtime_settings"] = [{"version": settings.policy_version}]
        if run.status == RunStatus.FAILED:
            return run
        with traced_operation(
            "semora.query.run",
            {
                "semora.run_id": run.id,
                "semora.session_id": run.session_id,
                "semora.domain": run.domain,
                "semora.trace_id": run.trace_id,
            },
        ):
            try:
                await self._invoke_graph(
                    run,
                    {
                        "run_id": run.id,
                        "settings": settings.model_dump(mode="json"),
                        "auto_align": auto_align,
                    },
                    config=self._config(run.id),
                )
            except asyncio.CancelledError:
                if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                    if run.agent_memory.get("user_cancelled"):
                        run.error = "查询已由用户取消"
                    else:
                        run.agent_memory["interruption"] = [
                            {"reason": "task_cancelled_without_user_request"}
                        ]
                        run.error = "服务处理被中断；已有口径和产物已保留，可以继续。"
                    self.emit(
                        run,
                        "execution.cancelled",
                        RunStatus.FAILED,
                        {"error": run.error},
                        "conversation_owner",
                    )
                raise
            except Exception as exc:
                if run.status not in {RunStatus.FAILED, RunStatus.AWAITING_APPROVAL}:
                    run.error = f"工作流编排失败：{exc}"
                    self.emit(
                        run,
                        "run.failed",
                        RunStatus.FAILED,
                        {"error": run.error},
                        "conversation_owner",
                    )
        return self.store.runs[run.id]

    async def start(
        self,
        session_id: str,
        question: str,
        authorization: AuthorizationContext | None = None,
        domain: str | None = None,
    ) -> QueryRun:
        run, settings = self._prepare_run(session_id, question, domain=domain, authorization=authorization)
        return await self._invoke(run, settings, auto_align=True)

    async def start_background(
        self,
        session_id: str,
        question: str,
        authorization: AuthorizationContext,
        domain: str | None = None,
        mode: str = "auto",
    ) -> QueryRun:
        run, settings = self._prepare_run(session_id, question, domain=domain, authorization=authorization, mode=mode)
        if run.status != RunStatus.FAILED:
            task = asyncio.create_task(
                self._invoke(run, settings, auto_align=False), name=f"semora-run-{run.id}"
            )
            self._tasks[run.id] = task

            def completed(finished):
                if self._tasks.get(run.id) is finished:
                    self._tasks.pop(run.id, None)

            task.add_done_callback(completed)
        return run

    def is_running(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    async def cancel(self, run_id: str) -> dict:
        run = self.store.runs[run_id]
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            return {
                "run_id": run_id,
                "accepted": False,
                "executor_cancelled": False,
                "task_cancelled": False,
                "status": run.status,
            }
        if run.status in {RunStatus.AWAITING_APPROVAL, RunStatus.CLARIFYING}:
            run.error = "查询已由用户取消"
            self.emit(
                run,
                "execution.cancelled",
                RunStatus.FAILED,
                {"error": run.error},
                "conversation_owner",
            )
            return {
                "run_id": run_id,
                "accepted": True,
                "executor_cancelled": False,
                "task_cancelled": False,
                "status": run.status,
            }
        executor_cancelled = False
        executor_cancel_error = None
        task = self._tasks.get(run_id)
        task_cancelled = bool(task and not task.done())
        # Stop scheduling further source queries before waiting on the remote
        # cancellation endpoint, which may be unavailable or slow.
        if task_cancelled:
            run.agent_memory["user_cancelled"] = [{"requested": True}]
            task.cancel()
        if run.status == RunStatus.EXECUTING:
            url = os.getenv("EXECUTOR_URL", "http://localhost:8002")
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.post(
                        f"{url}/internal/v1/executions/{run_id}/cancel",
                        headers={
                            "X-Semora-Internal-Token": os.getenv(
                                "INTERNAL_TOKEN", "semora-dev-internal"
                            )
                        },
                    )
                    response.raise_for_status()
                    executor_cancelled = bool(response.json().get("accepted"))
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                executor_cancel_error = type(exc).__name__
        if task_cancelled:
            try:
                await task
            except asyncio.CancelledError:
                pass
        return {
            "run_id": run_id,
            "accepted": executor_cancelled or task_cancelled,
            "executor_cancelled": executor_cancelled,
            "executor_cancel_error": executor_cancel_error,
            "task_cancelled": task_cancelled,
            "status": self.store.runs[run_id].status,
        }

    async def align(
        self,
        run: QueryRun,
        actor: str,
        answers: dict[str, str],
    ) -> QueryRun:
        if run.status != RunStatus.CLARIFYING or run.requirement_draft is None:
            raise ValueError("该查询当前不处于需求对齐状态")
        await self._invoke_graph(
            run,
            Command(resume={"confirmed": True, "answers": answers, "actor": actor}),
            config=self._config(run.id),
        )
        return self.store.runs[run.id]

    def start_alignment(self, run: QueryRun, actor: str, answers: dict[str, str]) -> QueryRun:
        if run.status != RunStatus.CLARIFYING or run.requirement_draft is None:
            raise ValueError("该查询当前不处于需求对齐状态，请查看任务的最新结果")
        if run.requirement_draft.technical_gaps and not run.requirement_draft.clarifications:
            raise ValueError("请先补齐已列出的配置缺口，再在聊天中继续分析当前需求")
        from .clarifications import active_questions, relevant_answers

        questions = active_questions(run.requirement_draft.clarifications, answers)
        answers = relevant_answers(run.requirement_draft.clarifications, answers)
        missing = [q.id for q in questions if q.blocking and not answers.get(q.id, "").strip()]
        if missing:
            raise ValueError("请先回答所有必填的澄清问题")
        if run.query_rehearsal:
            from .clarifications import active_gaps

            if active_gaps(run.query_rehearsal, answers):
                raise ValueError(
                    "所选分支存在卡片已列出的技术缺口，请先补齐配置；本次未提交口径答案"
                )
        run.agent_memory["pending_alignment"] = [{"actor": actor, "answers": answers}]
        self.emit(
            run,
            "alignment.submitted",
            RunStatus.UNDERSTANDING,
            {"summary": "已收到补充，后台继续分析", "draft_version": run.requirement_draft.version},
            "analyst",
        )
        self._queue_alignment(run, actor, answers)
        return run

    def _queue_alignment(self, run, actor, answers):
        async def resume():
            try:
                await self._invoke_graph(
                    run,
                    Command(resume={"confirmed": True, "answers": answers, "actor": actor}),
                    config=self._config(run.id),
                )
            except asyncio.CancelledError:
                if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                    if run.agent_memory.get("user_cancelled"):
                        run.error = "补充分析已取消"
                    else:
                        run.agent_memory["interruption"] = [
                            {"reason": "alignment_task_interrupted"}
                        ]
                        run.error = "补充分析被中断；已保存回答，可以继续当前需求。"
                    self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error})
                raise
            except Exception as exc:
                run.error = f"补充需求后台分析失败：{exc}"
                self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "analyst")
            finally:
                run.agent_memory.pop("pending_alignment", None)
                self.store.save_run(run)

        task = asyncio.create_task(resume(), name=f"semora-alignment-{run.id}")
        self._tasks[run.id] = task

        def completed(finished):
            if self._tasks.get(run.id) is finished:
                self._tasks.pop(run.id, None)

        task.add_done_callback(completed)

    def resume_pending_alignments(self):
        for run in self.store.runs.values():
            pending = run.agent_memory.get("pending_alignment")
            if pending and run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                self._queue_alignment(run, pending[0]["actor"], pending[0]["answers"])
            elif run.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CLARIFYING}:
                run.agent_memory["interruption"] = [
                    {"reason": "process_restart_without_running_task"}
                ]
                run.error = "上次处理因服务重启中断；已保存需求和已有产物，请继续当前需求。"
                self.emit(
                    run,
                    "run.interrupted",
                    RunStatus.FAILED,
                    {"summary": run.error, "recoverable": True},
                    "conversation_owner",
                )
