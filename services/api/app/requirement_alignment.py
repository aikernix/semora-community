"""Business confirmation and durable LangGraph interrupt/resume.

The graph node controls order; helpers own answer persistence, replay, analysis
and finalization. Keep interrupts in this node's call stack for durable replay.
"""

import hashlib
import json
from langgraph.types import interrupt
from semora_agent.graph import AgentState
from semora_agent.model import TaskSupersededError
from semora_contracts import ConfirmedFact, QueryPlan, RequirementDraft, RequirementSpec, RunStatus
from .clarifications import active_questions, relevant_answers, clarification_history


class RequirementAlignment:
    def __init__(self, workflow):
        self.workflow = workflow

    async def run(self, state: AgentState) -> dict:
        run = self.workflow._run(state)
        initial = state.get("alignment_initial_draft")
        draft = RequirementDraft.model_validate(initial) if initial else run.requirement_draft
        ontology = state.get("ontology_model")
        if draft is None or not ontology:
            return self._fail(
                run, "需求理解或本体上下文缺失，无法进入口径确认", "conversation_owner"
            )
        if self._configuration_required(run, draft):
            return {}
        decision = self._automatic_decision() if not draft.clarifications else self._ask(run, draft)
        while True:
            answers = decision.get("answers", {}) if isinstance(decision, dict) else {}
            answers = relevant_answers(
                draft.clarifications, answers if isinstance(answers, dict) else {}
            )
            visible = active_questions(draft.clarifications, answers)
            missing = [q.id for q in visible if q.blocking and not answers.get(q.id, "").strip()]
            if not isinstance(decision, dict) or not decision.get("confirmed") or missing:
                decision = self._ask(run, draft, missing=missing)
                continue
            if not draft.clarifications:
                break
            facts = self._persist_answers(run, draft, visible, answers)
            try:
                draft, plan = await self._analyse_answers(run, draft, facts, ontology)
            except Exception as exc:
                return self._analysis_failed(run, exc, "补充需求分析失败")
            run.requirement_draft, run.proposed_plan = draft, plan
            if self._configuration_required(run, draft, answered=True):
                return {}
            if draft.clarifications:
                decision = self._ask(run, draft, updated=True)
            else:
                self.workflow.emit(
                    run,
                    "alignment.resolved",
                    RunStatus.ALIGNING,
                    {
                        "summary": "业务歧义已澄清，自动进入 SQL 生成与校验",
                        "requirement": draft.model_dump(mode="json"),
                    },
                    "conversation_owner",
                )
                decision = self._automatic_decision(decision.get("actor"))
        plan = run.proposed_plan
        if plan is None:
            confirmed = "；".join(f"{f.field}={f.value}" for f in run.user_answers)
            try:
                outcome = await self.workflow._understand_and_author(
                    run,
                    f"{run.question}。用户已确认：{confirmed}",
                    ontology,
                    draft.data_scope,
                )
                plan = outcome.plan
            except Exception as exc:
                return self._analysis_failed(run, exc, "根据确认口径重新规划失败")
        if plan is None or plan.sql_program is None:
            return self._fail(run, "确认后的业务口径仍无法映射到已发布本体")
        self._finalize(run, draft, plan, run.user_answers, decision.get("actor"))
        return {"ontology_model": ontology}

    @staticmethod
    def _automatic_decision(actor=None):
        # No unanswered cards means confirmation is automatic, never select an
        # answer for a user. The former test-only switch cannot bypass cards.
        return {"confirmed": True, "answers": {}, "actor": actor or "conversation-owner"}

    def _configuration_required(self, run, draft, *, answered=False):
        if not draft.technical_gaps or draft.clarifications:
            return False
        self.workflow.emit(
            run,
            "rehearsal.configuration_required",
            RunStatus.CLARIFYING,
            {
                "summary": "业务答案已保留；请技术人员补齐已列出的配置缺口后继续。"
                if answered
                else "方案预演发现具体配置缺口；请补齐后继续当前需求，不需要猜测业务答案。",
                "gaps": [g.model_dump(mode="json") for g in draft.technical_gaps],
            },
        )
        return True

    def _ask(self, run, draft, *, missing=None, updated=False):
        requirement = draft.model_dump(mode="json")
        data = {"requirement": requirement}
        payload = {
            "run_id": run.id,
            "requirement_draft_version": draft.version,
            "requirement": requirement,
        }
        if missing is not None:
            data.update(
                blocking_count=len(missing),
                missing_question_ids=missing,
                message="请先回答所有关键业务口径。",
            )
            payload["missing_question_ids"] = missing
        elif updated:
            data["message"] = "请继续补充，或确认更新后的完整口径。"
        else:
            data["blocking_count"] = sum(q.blocking for q in draft.clarifications)
        # Interrupt replay presents the same card; it is not another finding.
        replayed = any(e.event == "alignment.required" and e.data == data for e in run.events)
        if not replayed:
            self.workflow.emit(
                run, "alignment.required", RunStatus.CLARIFYING, data, "conversation_owner"
            )
        return interrupt(payload)

    def _fail(self, run, message, actor="analyst"):
        run.error = message
        self.workflow.emit(run, "run.failed", RunStatus.FAILED, {"error": message}, actor)
        return {}

    def _analysis_failed(self, run, exc, prefix):
        if isinstance(exc, TaskSupersededError):
            return self.workflow._discard_superseded_task(run)
        return self._fail(run, f"{prefix}：{getattr(exc, 'public_message', str(exc))}")

    def _persist_answers(self, run, draft, visible, answers):
        gathered_facts = []
        for item in visible:
            value = str(answers.get(item.id, "")).strip()
            if not value:
                continue
            selected = next(
                (option for option in item.options if option.id == value or option.value == value),
                None,
            )
            gathered_facts.append(
                ConfirmedFact(
                    question_id=item.id,
                    field=item.field,
                    value=selected.value if selected else value,
                    label=selected.label if selected else value,
                )
            )
        # Persist answers before starting model work, including work
        # that later times out or fails preflight.
        declared = {item.id for item in draft.clarifications}
        saved = {
            fact.question_id: fact for fact in run.user_answers if fact.question_id not in declared
        }
        saved.update({fact.question_id: fact for fact in gathered_facts})
        run.user_answers = list(saved.values())
        submission_key = hashlib.sha256(
            json.dumps(
                {
                    "run": run.id,
                    "draft": draft.model_dump(mode="json"),
                    "answers": [f.model_dump(mode="json") for f in gathered_facts],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if submission_key not in clarification_history(run):
            run.clarification_history.append(
                {"key": submission_key, "stage": draft.clarification_stage}
            )
        self.workflow.store.save_run(run)
        return gathered_facts

    async def _analyse_answers(self, run, draft, facts, ontology_model):
        text = "；".join(f"{item.field}={item.value}" for item in facts)
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "answers": [f.model_dump(mode="json") for f in facts],
                    # Bind to the immutable submitted draft, not the mutable
                    # latest rehearsal. Durable replay starts with the old
                    # direction form after the business report is updated.
                    "draft": draft.model_dump(mode="json"),
                    "ontology": run.ontology.model_dump(mode="json") if run.ontology else None,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        cached = next((item for item in run.alignment_rounds if item["key"] == cache_key), None)
        if cached:
            next_draft = RequirementDraft.model_validate(cached["draft"])
            next_plan = QueryPlan.model_validate(cached["plan"]) if cached["plan"] else None
        else:
            outcome = await self.workflow._understand_and_author(
                run,
                self.workflow._analysis_question(run) + "。用户补充确认：" + text,
                ontology_model,
                draft.data_scope,
            )
            if outcome.requirement is None:
                raise ValueError("模型未返回补充分析")
            next_draft = outcome.requirement
            next_draft.version = draft.version + 1
            next_plan = outcome.plan
            run.alignment_rounds.append(
                {
                    "key": cache_key,
                    "answers": [item.model_dump(mode="json") for item in facts],
                    "draft": next_draft.model_dump(mode="json"),
                    "plan": next_plan.model_dump(mode="json") if next_plan else None,
                }
            )
        return next_draft, next_plan

    def _finalize(self, run, draft, plan, confirmed_facts, actor):
        actor = str(actor or "business-user")
        requirement_payload = {
            **{key: getattr(draft, key, []) for key in (
                "metrics", "dimensions", "filters", "aggregation_rules", "ordering", "sequence"
            )},
            "source_draft_version": draft.version,
            "summary": plan.sql_program.summary,
            "subject": draft.subject,
            "analysis_steps": [step.purpose for step in plan.sql_program.steps],
            "data_scope": draft.data_scope,
            "time_semantics": draft.time_semantics,
            "grain": draft.grain,
            "output_fields": list(plan.sql_program.steps[-1].output_columns),
            "assumptions": list(dict.fromkeys([*draft.assumptions, *plan.sql_program.assumptions])),
            "logical_plan": plan.logical_plan.model_dump(mode="json") if plan.logical_plan else None,
            "evidence": [e.model_dump(mode="json") for e in draft.evidence],
            "confirmed_facts": [f.model_dump(mode="json") for f in confirmed_facts],
            "confirmed_by": actor,
        }
        canonical = json.dumps(
            requirement_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        run.requirement = RequirementSpec(
            version=f"req-{draft.version}-{content_hash[:10]}",
            content_hash=content_hash,
            **requirement_payload,
        )
        run.plan = plan
        run.proposed_plan = None
        self.workflow.emit(
            run,
            "requirement.aligned",
            RunStatus.ALIGNED,
            {"requirement": run.requirement.model_dump(mode="json")},
            actor,
        )

        self.workflow.emit(
            run,
            "plan.ready",
            RunStatus.PLANNING,
            {
                "plan": run.plan.model_dump(mode="json"),
                "requirement_version": run.requirement.version,
            },
            "analyst",
        )
