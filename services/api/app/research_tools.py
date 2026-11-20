"""Tools available to the requirement-understanding Agent."""

import asyncio
from functools import wraps
import json
import re
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import uuid4

from langchain_core.tools import StructuredTool
from pydantic import Field, create_model
from semora_agent.model import TaskSupersededError
from semora_agent.model_diagnostics import debug_payload
from semora_contracts import KnowledgeResearchResult, KnowledgeResearchStep

from .knowledge import knowledge_service
from .prepared_context import supplement_context
from .knowledge_retrieval import knowledge_excerpt


class ResearchTools:
    def __init__(self, workflow, run, ontology, scope, *, knowledge_provider=None):
        self.workflow, self.run, self.ontology, self.scope = workflow, run, ontology, scope
        self.knowledge_provider = knowledge_provider or knowledge_service
        self.role = "analyst"
        self.context_state = None
        self.task_guard = None
        self.phase = ""
        self.phase_id = self.iteration = self.role_iteration = None
        self.value_probes = {}
        self.value_probe_lock = asyncio.Lock()

    def start_phase(self, label):
        self.phase = label
        self.phase_id = 1 + sum(e.event == "agent.phase.started" for e in self.run.events)
        self.event("agent.phase.started", {"summary": f"进入{label}阶段"})

    def event(self, name, data):
        data = dict(data)
        if name == "agent.model.started":
            previous = self.run.continuation or {}
            self.iteration = (
                1
                + previous.get("model_round_offset", 0)
                + sum(e.event == name for e in self.run.events)
            )
            self.role_iteration = (
                1
                + previous.get("role_rounds", {}).get(self.role, 0)
                + sum(e.event == name and e.data.get("agent") == self.role for e in self.run.events)
            )
        if self.iteration is not None:
            data.update(iteration=self.iteration, role_iteration=self.role_iteration)
        self.workflow.emit(
            self.run,
            name,
            self.run.status,
            {**data, "agent": self.role, "phase": self.phase, "phase_id": self.phase_id},
            self.role,
        )

    def _tool(self, function):
        @wraps(function)
        async def invoke(*args, **kwargs):
            if self.task_guard:
                self.task_guard()
            goal = kwargs.pop("analysis_goal", "")
            call_id = str(uuid4())
            debug_payload(
                "tool.call",
                {
                    "call_id": call_id,
                    "tool": function.__name__,
                    "args": args,
                    "kwargs": kwargs,
                    "goal": goal,
                },
            )
            self.event(
                "agent.tool.started",
                {"tool": function.__name__, "summary": f"调用 {function.__name__}"},
            )
            try:
                result = await function(*args, **kwargs)
                if self.task_guard:
                    self.task_guard()
            except TaskSupersededError:
                raise
            except Exception as exc:
                result = {
                    "available": False,
                    "error": type(exc).__name__,
                    "message": str(exc)[:600] if isinstance(exc, ValueError) else "资料工具暂不可用，保留已有资料",
                }
            self.event(
                "agent.tool.completed",
                {
                    "tool": function.__name__,
                    "summary": str(result.get("message") or f"{function.__name__} 已返回资料"),
                    "available": result.get("available", True),
                },
            )
            debug_payload(
                "tool.result", {"call_id": call_id, "tool": function.__name__, "result": result}
            )
            return json.dumps(result, ensure_ascii=False, default=str)

        tool = StructuredTool.from_function(coroutine=invoke)
        tool.args_schema = create_model(
            f"{function.__name__}Input",
            __base__=tool.args_schema,
            analysis_goal=(
                str,
                Field(default="", max_length=300, description="本次需要核实的具体资料缺口。"),
            ),
        )
        tool.args_schema.model_config = {**tool.args_schema.model_config, "extra": "forbid"}
        tool.args_schema.model_rebuild(force=True)
        tool.handle_validation_error = lambda error: json.dumps(
            {"available": False, "error": "invalid_arguments", "message": str(error)},
            ensure_ascii=False,
        )
        return tool

    def tools(self):
        async def request_context(
            objects: Annotated[list[str], Field(min_length=1, max_length=4)],
            purpose: Literal["output", "filter", "time", "join", "aggregation", "sort"],
            reason: str,
        ) -> dict:
            """补齐完成当前需求必需的对象资料，说明用途；资料足够时直接提交。"""
            if not self.context_state:
                return {"available": False, "message": "准备资料缺失"}
            from .query_context_wire import model_material
            result = supplement_context(
                self.workflow,
                self.run,
                self.ontology,
                self.context_state,
                objects,
                purpose,
                reason,
                self.scope,
            )
            if "prepared_context" in result:
                result = {**result, "prepared_context": model_material(result["prepared_context"])}
            return result

        async def read_evidence() -> dict:
            """读取本次需求已取得的知识证据，不重新检索。"""
            from .model_context import project_knowledge

            return project_knowledge(
                {"evidence": [e.model_dump(mode="json") for e in self.run.evidence]}, self.run
            )

        functions = [request_context]
        if self.context_state and self.context_state["packet"].get("schemas"):
            functions.append(self.inspect_field_values)
        if self.run.evidence:
            functions.append(read_evidence)
        if self.ontology.get("knowledgeBaseIds"):
            functions.extend(self.knowledge_tools())
        return [self._tool(f) for f in functions]

    async def inspect_field_values(
        self,
        object_name: str,
        field: str,
        reason: Annotated[str, Field(min_length=1, max_length=300)],
    ) -> dict:
        """核实已选字段的实际标签、特殊值或空值；用户要求检查某类记录时先用此工具。只返回有限去重值，不接受 SQL。"""
        from .field_value_probe import inspect_values

        if not reason.strip():
            raise ValueError("请说明必须核实的取值及其对本次结果的影响")
        async with self.value_probe_lock:
            if self.task_guard:
                self.task_guard()
            key = (object_name, field)
            if key in self.value_probes:
                return self.value_probes[key]
            if len(self.value_probes) >= 2:
                return {"available": False, "message": "本阶段字段取值探查预算已用完"}
            self.value_probes[key] = {"available": False, "message": "此字段探查未完成，不重复执行"}
            result = await inspect_values(
                self.workflow, self.run, self.ontology, self.context_state["packet"], object_name, field
            )
            if self.task_guard:
                self.task_guard()
            self.value_probes[key] = result
            if result["available"]:
                observations = self.context_state["packet"].setdefault("field_value_observations", [])
                observations.append(result)
                self.event("context.field_values.observed", {
                    "object": object_name, "field": field,
                    "value_count": len(result["values"]), "complete": result["complete"],
                    "summary": "已只读核实字段取值",
                })
            return result

    def remember_page(self, kb_id, page):
        """Make newly retrieved knowledge available to the following SQL role."""
        if self.context_state is None:
            return
        knowledge = self.context_state["packet"].setdefault("knowledge", [])
        if any(
            k.get("knowledge_base_id") == kb_id and k.get("slug") == page.get("slug")
            for k in knowledge
        ):
            return
        knowledge.append(
            {
                "knowledge_base_id": kb_id,
                "slug": page.get("slug"),
                "title": page.get("title"),
                **knowledge_excerpt(
                    page.get("content") or page.get("summary"),
                    self.context_state["packet"].get("knowledge_keywords", []),
                ),
                "source_refs": page.get("source_refs") or [],
            }
        )

    def knowledge_tools(self):
        kb_ids = self.ontology.get("knowledgeBaseIds") or []
        discoveries = self.run.agent_memory.setdefault("knowledge_discovery", [])

        def resolve_kb(kb_id):
            # With exactly one binding, routing is a deterministic fact, not
            # something the model needs to reconstruct from a UUID.
            if not kb_id and len(kb_ids) == 1:
                return kb_ids[0]
            return kb_id if kb_id in kb_ids else None

        def scope_error():
            return {
                "available": False,
                "error": "invalid_knowledge_base",
                "allowed_kb_ids": kb_ids,
                "message": "知识库ID缺失或不属于当前绑定范围。请使用 allowed_kb_ids 中的ID；未访问其他知识库。",
            }

        def allowed(kb_id, kind, ref):
            return kb_id in kb_ids and any(
                r == {"kb_id": kb_id, "kind": kind, "ref": ref} for r in discoveries
            )

        def remember(kb_id, kind, ref):
            value = {"kb_id": kb_id, "kind": kind, "ref": ref}
            if ref and value not in discoveries:
                discoveries.append(value)

        def research_step(tool, summary):
            if self.run.knowledge_research is None:
                self.run.knowledge_research = KnowledgeResearchResult(
                    provider=self.knowledge_provider.provider,
                    agent_id="semora-analyst-react",
                    session_id=self.run.id,
                )
            self.run.knowledge_research.steps.append(
                KnowledgeResearchStep(kind="tool_result", tool=tool, summary=summary)
            )
            self.run.knowledge_research.evidence = self.run.evidence

        def missing_information(reason):
            if not reason.strip():
                return {
                    "available": False,
                    "error": "missing_information_required",
                    "message": "已有知识足够时直接提交方案。只有具体定义、条件或冲突尚未解决才继续取证；请用reason说明缺什么及对查询的影响。本次未访问知识库。",
                }
            return None

        async def search_wiki(
            query: Annotated[
                str,
                Field(
                    min_length=1,
                    max_length=64,
                    description="每次只填一个短关键词、别名或表名。禁止用空格/逗号/竖线拼多个关键词。看完本次结果后再调用下一次。",
                ),
            ],
            kb_id: str = "",
            reason: Annotated[
                str,
                Field(
                    description="已有知识尚缺的具体定义、条件或冲突，以及对当前查询的影响；资料足够时不调用。"
                ),
            ] = "",
        ) -> dict:
            """仅在已有知识不足时，用一个关键词补检索绑定知识库，不为扩大主题继续搜索。"""
            kb_id = resolve_kb(kb_id)
            if kb_id is None:
                return scope_error()
            missing = missing_information(reason)
            if missing:
                return missing
            keyword = query.strip()
            terms = [term for term in re.split(r"[\s,，、;；|]+", keyword) if term]
            if not keyword or len(terms) != 1 or re.search(r"[\s,，、;；|]", keyword):
                return {
                    "available": False,
                    "error": "single_keyword_required",
                    "suggested_keywords": terms[:6],
                    "message": "一次只允许检索一个关键词。请从 suggested_keywords 中选一个单独调用，观察返回后再决定下一个词；本次没有发出检索请求。",
                }
            # Escape regex syntax: this Agent tool exposes literal keyword
            # search, not the upstream's raw regular-expression API.
            rows = await self.knowledge_provider._wiki_search(kb_id, re.escape(keyword), limit=8)
            for row in rows:
                remember(kb_id, "page", row.get("slug"))
            message = f"检索“{keyword}”：返回 {len(rows)} 个 Wiki 页面。"
            if not rows:
                message += "仅此关键词零命中，不能判定整个知识库没有内容。仅在核心信息仍缺失时换短词或别名补查；已有资料足够则提交方案。"
            else:
                message += "请先阅读相关页面，再按证据缺口决定是否搜索下一个关键词。"
            if len(rows) == 8:
                message += "已达到本次返回上限，8 不是全库命中总数；需要时换更具体的关键词。"
            research_step("search_wiki", message)
            return {
                "keyword": keyword,
                "count": len(rows),
                "limit": 8,
                "possibly_truncated": len(rows) == 8,
                "message": message,
                "pages": [
                    {
                        "slug": r.get("slug"),
                        "title": r.get("title"),
                        "summary": str(r.get("summary") or r.get("content") or "")[:600],
                    }
                    for r in rows
                ],
            }

        async def read_wiki(
            slug: str,
            kb_id: str = "",
            reason: Annotated[
                str, Field(description="需要通过该页面补齐的具体信息及其对查询的影响。")
            ] = "",
        ) -> dict:
            """已有知识不足时，沿已发现的页面或关联链接补读；不自动遍历知识图谱。"""
            kb_id = resolve_kb(kb_id)
            if kb_id is None:
                return scope_error()
            missing = missing_information(reason)
            if missing:
                return missing
            if not allowed(kb_id, "page", slug):
                return {"available": False, "message": "该页面尚未被检索发现，请先搜索入口"}
            page = await self.knowledge_provider._wiki_page(kb_id, slug)
            links = (page.get("out_links") or []) + (page.get("in_links") or [])
            for link in links:
                remember(kb_id, "page", link.get("slug") if isinstance(link, dict) else link)
            for source in page.get("source_refs") or []:
                if isinstance(source, str):
                    remember(kb_id, "source", source)
            page = {**page, "slug": page.get("slug") or slug}
            self.remember_page(kb_id, page)
            evidence = self.knowledge_provider._page_evidence(kb_id, page)
            if not any(
                e.knowledge_base_id == kb_id and e.locator == evidence.locator
                for e in self.run.evidence
            ):
                self.run.evidence.append(evidence)
            research_step("read_wiki", f"阅读 {evidence.title}，取得来源和关联页面")
            return {
                "page": {
                    "slug": slug,
                    "title": page.get("title"),
                    "content": str(page.get("content") or "")[:8000],
                    "links": links[:30],
                    "source_refs": page.get("source_refs") or [],
                }
            }

        async def read_source(
            document_id: str,
            kb_id: str = "",
            page: int = 1,
            reason: Annotated[
                str, Field(description="必须核实的定义、条件或冲突及其对查询的影响。")
            ] = "",
        ) -> dict:
            """存在具体证据缺口或冲突时，回溯已读Wiki引用的原文；资料足够时不调用。"""
            kb_id = resolve_kb(kb_id)
            if kb_id is None:
                return scope_error()
            missing = missing_information(reason)
            if missing:
                return missing
            if not allowed(kb_id, "source", document_id):
                return {"available": False, "message": "原文没有出现在已读取的证据引用中，不能访问"}
            chunks, _ = await self.knowledge_provider._request(
                "GET",
                f"/chunks/{quote(document_id, safe='')}",
                params={"page": max(1, min(page, 20)), "page_size": 10},
            )
            if self.context_state is not None and isinstance(chunks, list):
                self.remember_page(
                    kb_id,
                    {
                        "slug": f"source:{document_id}:{page}",
                        "title": f"原文 {document_id} · 第 {page} 页",
                        "content": "\n\n".join(
                            str(c.get("content") or "") for c in chunks if isinstance(c, dict)
                        ),
                        "source_refs": [
                            {"document_id": document_id, "chunk_id": c.get("id")}
                            for c in chunks
                            if isinstance(c, dict)
                        ],
                    },
                )
            research_step("read_source", f"回溯文档 {document_id} 第 {page} 页分块")
            return {
                "chunks": [
                    {"id": r.get("id"), "content": str(r.get("content") or "")[:3000]}
                    for r in chunks
                    if isinstance(r, dict)
                ]
                if isinstance(chunks, list)
                else []
            }

        return [search_wiki, read_wiki, read_source]
