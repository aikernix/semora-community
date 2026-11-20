from __future__ import annotations
from semora_agent.prompts import PROBE_JSON, PROBE_TOOL

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi.exceptions import RequestValidationError
from fastapi import FastAPI, File, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from uuid import uuid4
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from semora_telemetry import configure_telemetry

from semora_contracts import (
    AuthorizationContext,
    CandidateExtractionResult,
    OntologyCandidate,
    QueryRun,
    RunStatus,
    SystemSettings,
)

from .store import store
from .workflow import Workflow
from .auth import Principal, enforce_roles, oidc_verifier
from .knowledge import KnowledgeCoreError, knowledge_service
from .ontology import OntologyCoreError, ontology_service
from .datasources import datasource_service
from .datasources import DataSourceRecord
from semora_contracts.database import DatabaseConnection, PRODUCTS
from .knowledge_settings import KnowledgeConfigInput, knowledge_settings
from .settings import settings_service
from .model_settings import model_settings, ModelConfigInput
from .candidate_extraction import CandidateExtractionError, candidate_extractor
from .database_routes import router as database_router, modeling_service


@asynccontextmanager
async def lifespan(_: FastAPI):
    dsn = os.getenv("LANGGRAPH_DATABASE_URL")
    context_manager = None
    if dsn:
        context_manager = AsyncPostgresSaver.from_conn_string(dsn)
        saver = await context_manager.__aenter__()
        workflow.configure_checkpointer(saver)
    workflow.resume_pending_alignments()
    modeling_service.resume()
    try:
        yield
    finally:
        if context_manager is not None:
            await context_manager.__aexit__(None, None, None)


app = FastAPI(title="Semora API", version="0.1.0", lifespan=lifespan)
@app.exception_handler(RequestValidationError)
async def private_validation_error(request, exc):
    # Pydantic root-validator errors can otherwise echo the full password-bearing input.
    return JSONResponse(status_code=422, content={"detail":[{k:e[k] for k in ("loc","msg","type") if k in e} for e in exc.errors()]})

configure_telemetry(app, "semora-api")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(database_router)
workflow = Workflow(store)


@app.middleware("http")
async def oidc_authentication(request: Request, call_next):
    if os.getenv("AUTH_REQUIRED", "false").lower() == "true" and request.url.path.startswith("/api/v1"):
        try:
            request.state.principal = oidc_verifier.verify(request.headers.get("Authorization"))
            enforce_roles(request.state.principal, request.method, request.url.path)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


class SessionCreate(BaseModel):
    title: str = "新查询"
    domain: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    requirement_form: dict[str, str] = Field(default_factory=dict)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class KnowledgeResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    knowledge_base_ids: list[str] = Field(min_length=1, max_length=20)


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    mode: Literal['auto', 'continue', 'new'] = 'auto'


class AlignmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict[str, str] = Field(default_factory=dict)
    draft_version: int | None = Field(default=None,ge=1)


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    reason: str = "已核对业务用途和数据范围"


class DataSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    type: Literal["postgresql", "oracle", "mysql", "sqlserver", "oceanbase", "goldendb", "vastbase", "jdbc"] = "postgresql"
    secret_ref: str | None = Field(default=None, pattern=r"^DATASOURCE_[A-Z0-9_]+_URL$", max_length=200)
    connection: "DataSourceConnection | None" = None
    test_receipt: str | None = Field(default=None, max_length=2000)


class DataSourceRename(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)


DataSourceConnection = DatabaseConnection

DataSourceCreate.model_rebuild()


def connection_payload(body: DataSourceCreate):
    if body.connection is None or body.connection.type != body.type:
        raise HTTPException(422, "请填写与数据库类型一致的连接参数")
    return {**body.connection.model_dump(exclude={"password"}),
            "password": body.connection.password.get_secret_value()}


def _connection_receipt_secret() -> bytes:
    return os.getenv(
        "DATASOURCE_TEST_RECEIPT_SECRET", os.getenv("INTERNAL_TOKEN", "semora-dev-internal")
    ).encode()


def _connection_fingerprint(body: DataSourceCreate) -> str:
    canonical = json.dumps(
        {"type": body.type, "connection": connection_payload(body)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(_connection_receipt_secret(), canonical, hashlib.sha256).hexdigest()


def _issue_connection_receipt(body: DataSourceCreate) -> str:
    payload = json.dumps(
        {"fingerprint": _connection_fingerprint(body), "expires_at": int(time.time()) + 600},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(_connection_receipt_secret(), payload, hashlib.sha256).digest()
    encoded_payload = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return encoded_payload + "." + encoded_signature


def _verify_connection_receipt(body: DataSourceCreate) -> None:
    if not body.test_receipt:
        raise HTTPException(422, "请先完成当前连接参数的连接测试")
    try:
        encoded_payload, encoded_signature = body.test_receipt.split(".", 1)
        payload = base64.urlsafe_b64decode(
            (encoded_payload + "=" * (-len(encoded_payload) % 4)).encode()
        )
        signature = base64.urlsafe_b64decode(
            (encoded_signature + "=" * (-len(encoded_signature) % 4)).encode()
        )
        expected = hmac.new(_connection_receipt_secret(), payload, hashlib.sha256).digest()
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("invalid receipt payload")
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error):
        raise HTTPException(422, "连接测试凭证无效，请重新测试") from None
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(422, "连接测试凭证无效，请重新测试")
    if int(value.get("expires_at", 0)) < int(time.time()):
        raise HTTPException(422, "连接测试凭证已过期，请重新测试")
    if not hmac.compare_digest(str(value.get("fingerprint", "")), _connection_fingerprint(body)):
        raise HTTPException(422, "连接参数已变化，请重新测试")


@app.get("/api/v1/settings/knowledge")
def get_knowledge_config():
    return knowledge_settings.public()


@app.put("/api/v1/settings/knowledge")
def save_knowledge_config(body: KnowledgeConfigInput):
    try:
        return knowledge_settings.save(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/settings/knowledge/test")
async def test_knowledge_config(body: KnowledgeConfigInput):
    try:
        return await knowledge_settings.candidate(body).status()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except KnowledgeCoreError as exc:
        raise HTTPException(502, str(exc)) from exc


def authorization_context(request: Request) -> AuthorizationContext:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return AuthorizationContext(
            subject="demo-user",
            username="演示分析员",
            roles=["business_user", "analyst"],
            branch_ids=[1, 2, 3],
            sensitivity_clearance="confidential",
        )
    raw_branch_ids = principal.claims.get("branch_ids", [])
    if isinstance(raw_branch_ids, str):
        raw_branch_ids = [item.strip() for item in raw_branch_ids.split(",") if item.strip()]
    try:
        branch_ids = sorted({int(item) for item in raw_branch_ids})
    except (TypeError, ValueError) as exc:
        raise HTTPException(403, "invalid branch_ids authorization claim") from exc
    clearance = principal.claims.get("sensitivity_clearance", "internal")
    if clearance not in {"internal", "confidential", "restricted"}:
        raise HTTPException(403, "invalid sensitivity_clearance authorization claim")
    sources = principal.claims.get('data_source_ids')
    if sources is not None and (not isinstance(sources, list) or any(not isinstance(s, str) or not s for s in sources)):
        raise HTTPException(403, 'invalid data_source_ids authorization claim')
    return AuthorizationContext(
        subject=principal.subject,
        username=principal.username,
        roles=sorted(principal.roles),
        branch_ids=branch_ids,
        sensitivity_clearance=clearance,
        allowed_data_source_ids=sources,
        cross_organization=bool(principal.claims.get("cross_organization", False)),
        policy_version=str(principal.claims.get("abac_policy_version", "abac-v1")),
    )


def request_principal(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


def can_access_session(session, principal: Principal | None) -> bool:
    return (
        principal is None
        or "admin" in principal.roles
        or session.owner_subject is None
        or session.owner_subject == principal.subject
    )


def require_session_access(session_id: str, request: Request):
    session = store.sessions.get(session_id)
    if session is None or not can_access_session(session, request_principal(request)):
        raise HTTPException(404, "session not found")
    return session


def can_access_run(run: QueryRun, principal: Principal | None) -> bool:
    if principal is None or "admin" in principal.roles:
        return True
    return run.authorization is not None and run.authorization.subject == principal.subject


def require_run_access(run_id: str, request: Request) -> QueryRun:
    run = store.runs.get(run_id)
    if run is None or not can_access_run(run, request_principal(request)):
        raise HTTPException(404, "run not found")
    return run


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "semora-api", "workflow": workflow.runtime_info()}


@app.get("/api/v1/sessions")
def list_sessions(request: Request) -> list[dict]:
    principal = request_principal(request)
    return [
        {
            "id": item.id,
            "title": item.title,
            "domain": item.domain,
            "created_at": item.created_at,
            "run_count": len(item.run_ids),
            "latest_status": next((store.runs[run_id].status for run_id in reversed(item.run_ids) if run_id in store.runs), None),
            "requirement_form": item.requirement_form,
        }
        for item in sorted(store.sessions.values(), key=lambda value: value.created_at, reverse=True)
        if can_access_session(item, principal)
    ]


@app.get("/api/v1/domains")
async def list_domains() -> list[dict]:
    try:
        return await ontology_service.list_drafts()
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.post("/api/v1/sessions", status_code=201)
async def create_session(body: SessionCreate, request: Request) -> dict:
    try:
        await ontology_service.get_draft(body.domain)
    except OntologyCoreError as exc:
        raise HTTPException(422, f"业务域不存在或不可用：{exc}") from exc
    principal = request_principal(request)
    session = store.create_session(body.title, body.domain, principal.subject if principal else None)
    session.requirement_form = body.requirement_form
    store._persist_session(session)
    return {
        "id": session.id,
        "title": session.title,
        "domain": session.domain,
        "created_at": session.created_at,
        "run_count": 0,
    }


@app.delete("/api/v1/sessions/{session_id}")
async def delete_query_session(session_id: str, request: Request) -> dict[str, bool]:
    session = require_session_access(session_id, request)
    principal = request_principal(request)
    if principal is not None and session.owner_subject is None and "admin" not in principal.roles:
        raise HTTPException(403, "只有管理员可以删除没有归属人的查询。")
    inactive = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CLARIFYING, RunStatus.AWAITING_APPROVAL}
    if any(
        run_id in store.runs and (
            store.runs[run_id].status not in inactive or workflow.is_running(run_id)
        )
        for run_id in session.run_ids
    ):
        raise HTTPException(409, "该查询仍在运行，请等待完成或取消后再删除。")
    store.archive_session(session)
    return {"deleted": True}


@app.get("/api/v1/sessions/{session_id}/runs", response_model=list[QueryRun])
def list_session_runs(
    session_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[QueryRun]:
    session = require_session_access(session_id, request)
    return [store.runs[run_id] for run_id in reversed(session.run_ids) if run_id in store.runs][:limit]


@app.put('/api/v1/sessions/{session_id}/requirement')
def update_session_requirement(session_id: str, body: SessionCreate, request: Request):
    session=require_session_access(session_id,request)
    if session.run_ids:
        raise HTTPException(409,'需求单已经开始分析，请在对话中补充或新建需求')
    if session.domain!=body.domain:
        raise HTTPException(409,'业务域已绑定，请新建需求切换业务域')
    session.title=body.title
    session.requirement_form=body.requirement_form
    store._persist_session(session)
    return {'id':session.id,'domain':session.domain}


@app.post("/api/v1/sessions/{session_id}/messages", response_model=QueryRun, status_code=202)
async def send_message(session_id: str, body: MessageCreate, request: Request) -> QueryRun:
    session = require_session_access(session_id, request)
    # Approval is an administrator-owned system policy. Query callers cannot
    # weaken it on an individual request.
    if model_settings.dsn and not model_settings.public()['configured']:
        raise HTTPException(409,'请先在模型配置页面设置并测试大模型连接')
    return await workflow.start_background(
        session_id,
        body.question,
        authorization_context(request),
        domain=session.domain,
        mode=body.mode,
    )


@app.get("/api/v1/settings", response_model=SystemSettings)
def get_settings() -> SystemSettings:
    return settings_service.get()


@app.get("/api/v1/settings/limits")
def get_settings_limits() -> dict[str, int]:
    return {}


@app.put("/api/v1/settings", response_model=SystemSettings)
def update_settings(body: SystemSettings, request: Request) -> SystemSettings:
    principal = getattr(request.state, "principal", None)
    if principal is not None and "admin" not in principal.roles:
        raise HTTPException(403, "only admin can update system settings")
    actor = principal.username if principal is not None else "demo.admin"
    try:
        return settings_service.update(body, actor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/runs/{run_id}", response_model=QueryRun)
def get_run(run_id: str, request: Request) -> QueryRun:
    return require_run_access(run_id, request)


@app.post("/api/v1/runs/{run_id}/alignment", response_model=QueryRun, status_code=202)
async def confirm_run_alignment(
    run_id: str,
    body: AlignmentInput,
    request: Request,
) -> QueryRun:
    run = require_run_access(run_id, request)
    principal = request_principal(request)
    if body.draft_version is not None and run.requirement_draft and body.draft_version!=run.requirement_draft.version:
        raise HTTPException(409,'澄清卡片已更新，请刷新后回答最新的问题')
    actor = principal.username if principal is not None else "演示业务用户"
    try:
        return workflow.start_alignment(run, actor, body.answers)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/v1/model/status')
def model_status():
    return {'configured':model_settings.public()['configured']}


@app.get('/api/v1/settings/model')
def get_model_config():
    return model_settings.public()


@app.put('/api/v1/settings/model')
def save_model_config(body: ModelConfigInput, request: Request):
    principal=getattr(request.state,'principal',None)
    if principal is not None and 'admin' not in principal.roles:
        raise HTTPException(403,'only admin can update model settings')
    try:
        return model_settings.save(body)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


@app.post('/api/v1/settings/model/test')
async def test_model_config(body: ModelConfigInput, request: Request):
    principal=getattr(request.state,'principal',None)
    if principal is not None and 'admin' not in principal.roles:
        raise HTTPException(403,'only admin can test model settings')
    try:
        candidate=model_settings.candidate(body)
        candidate.timeout_seconds=20
        result=await candidate.complete_json(PROBE_JSON,'连接测试。')
        if result.get('ok') is not True:
            raise ValueError('模型未返回要求的 JSON；请检查是否支持 JSON 输出')
        probe = await candidate.complete_messages(
            [{'role':'user','content':PROBE_TOOL}],
            [{'type':'function','function':{'name':'semora_probe','description':'验证模型的原生工具调用能力','parameters':{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']}}}],
            tool_choice={'type':'function','function':{'name':'semora_probe'}},
        )
        calls = probe.get('tool_calls') or []
        if not any(c.get('function',{}).get('name') == 'semora_probe' and json.loads(c['function'].get('arguments','{}')).get('ok') is True for c in calls):
            raise ValueError('模型未返回有效工具调用；多轮 Agent 需要支持 tool calling 的模型')
        return {'ok':True,'model':candidate.model,'tool_calling':True}
    except Exception as exc:
        from semora_agent.model import ModelError
        if isinstance(exc,(ValueError,ModelError)):
            raise HTTPException(422,str(exc)) from exc
        raise HTTPException(502,'模型连接测试失败') from exc


@app.get("/api/v1/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    heartbeat: bool = Query(default=False),
) -> StreamingResponse:
    require_run_access(run_id, request)

    from .events_stream import stream_events

    return StreamingResponse(
        stream_events(store, run_id, after, request.is_disconnected, with_heartbeat=heartbeat),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def raise_knowledge_error(exc: KnowledgeCoreError) -> None:
    raise HTTPException(503, str(exc)) from exc


def raise_ontology_error(exc: OntologyCoreError) -> None:
    raise HTTPException(exc.status_code, str(exc)) from exc


@app.get("/api/v1/knowledge/status")
async def knowledge_status() -> dict:
    try:
        return await knowledge_service.status()
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)


@app.get("/api/v1/knowledge-bases")
async def knowledge_bases() -> list[dict]:
    try:
        return await knowledge_service.list_knowledge_bases()
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)


@app.post("/api/v1/knowledge-bases", status_code=201)
async def create_knowledge_base(body: KnowledgeBaseCreate) -> dict:
    try:
        return await knowledge_service.create_knowledge_base(body.name, body.description)
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)


@app.get("/api/v1/knowledge-bases/{kb_id}/knowledge")
async def list_knowledge(
    kb_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    try:
        return await knowledge_service.list_knowledge(kb_id, page, page_size)
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)


@app.post("/api/v1/knowledge-bases/{kb_id}/knowledge/file", status_code=202)
async def upload_knowledge_file(
    kb_id: str,
    file: UploadFile = File(...),
) -> dict:
    max_bytes = int(os.getenv("KNOWLEDGE_UPLOAD_MAX_BYTES", str(25 * 1024 * 1024)))
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(413, f"文档超过 {max_bytes // 1024 // 1024} MB 限制")
    if not file.filename:
        raise HTTPException(400, "缺少文件名")
    try:
        document = await knowledge_service.upload_document(
            kb_id,
            file.filename,
            content,
            file.content_type or "application/octet-stream",
        )
        return {"provider": knowledge_service.provider, "document": document}
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)


@app.get("/api/v1/knowledge/search")
async def knowledge_search(
    q: str = Query(min_length=1),
    knowledge_base_id: str | None = None,
) -> dict:
    try:
        evidence = await knowledge_service.search_evidence(q, [knowledge_base_id] if knowledge_base_id else None)
        return {
            "provider": knowledge_service.provider,
            "results": [item.model_dump(mode="json") for item in evidence],
        }
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)


@app.post("/api/v1/knowledge/research")
async def knowledge_research(body: KnowledgeResearchInput) -> dict:
    try:
        result = await knowledge_service.research(body.query, body.knowledge_base_ids)
        return result.model_dump(mode="json")
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)


@app.get("/api/v1/knowledge/{knowledge_id}")
async def knowledge_detail(knowledge_id: str) -> dict:
    try:
        return await knowledge_service.get_knowledge(knowledge_id)
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)


@app.post("/api/v1/knowledge/{knowledge_id}/retry", status_code=202)
async def retry_knowledge_ingestion(knowledge_id: str) -> dict:
    try:
        return await knowledge_service.retry_document(knowledge_id)
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)


@app.get("/api/v1/knowledge/{knowledge_id}/content")
async def download_knowledge_content(knowledge_id: str) -> Response:
    try:
        content, file_name, content_type, content_hash = await knowledge_service.download_document(knowledge_id)
        safe_name = file_name.replace("\r", "").replace("\n", "")
        return Response(
            content=content,
            media_type=content_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_name)}",
                "X-Semora-Content-Hash": content_hash,
            },
        )
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)


@app.post(
    "/api/v1/knowledge/{knowledge_id}/ontology-candidates",
    response_model=CandidateExtractionResult,
    status_code=201,
)
async def extract_ontology_candidates(knowledge_id: str, domain: str = Query(...,pattern=r"^[a-z][a-z0-9_]{1,99}$")) -> dict:
    try:
        bundle = await knowledge_service.get_document_chunks(knowledge_id)
        draft = await ontology_service.get_draft(domain)
        proposals, mode = await candidate_extractor.extract(bundle, draft, domain)
        candidates = [await ontology_service.create_candidate(value) for value in proposals]
        return {
            "provider": "semora-native",
            "extractor_mode": mode,
            "domain": domain,
            "document_id": knowledge_id,
            "count": len(candidates),
            "candidates": candidates,
        }
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)
    except CandidateExtractionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.get("/api/v1/ontology/drafts")
async def ontology_drafts() -> list[dict]:
    try:
        return await ontology_service.list_drafts()
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


class DomainCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    domain: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    name: str = Field(min_length=1, max_length=200)


@app.post("/api/v1/ontology/domains", status_code=201)
async def create_ontology_domain(body: DomainCreate):
    try:
        return await ontology_service.create_domain(body.domain, body.name)
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.delete("/api/v1/ontology/domains/{domain}")
async def delete_ontology_domain(domain: str = Path(pattern=r"^[a-z][a-z0-9_]{1,99}$")) -> dict[str, bool]:
    try:
        return await ontology_service.delete_domain(domain)
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.get("/api/v1/ontology/configurations")
async def ontology_configurations():
    try:
        return await ontology_service.list_drafts()
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.get("/api/v1/ontology/configurations/{domain}")
async def ontology_configuration(domain: str):
    try:
        return await ontology_service.get_configuration(domain)
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.put("/api/v1/ontology/configurations/{domain}")
async def save_ontology_configuration(domain: str, body: dict):
    objects = body.get("objects")
    if not isinstance(objects, list) or not objects or any(not isinstance(obj, dict) for obj in objects):
        raise HTTPException(422, "请至少配置一个对象，并为对象选择数据源")
    for identifier in {str(obj.get("dataSourceId") or body.get("dataSourceId") or "") for obj in objects}:
        try:
            source = datasource_service.repository.get(identifier)
        except KeyError as exc:
            raise HTTPException(422, "对象引用了未登记的数据源，请重新选择数据源") from exc
        if source.status != "healthy" or source.access != "read_only":
            raise HTTPException(422, f"请先完成数据源连接测试：{source.name}")
    try:
        return await ontology_service.save_configuration(domain, body)
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.get('/api/v1/ontology/publication-status')
async def ontology_publication_status(domain: str) -> dict:
    try:
        draft = await ontology_service.get_draft(domain)
        try:
            reference, model = await ontology_service.latest_snapshot_bundle(domain)
        except OntologyCoreError as exc:
            if exc.status_code != 404:
                raise
            reference, model = None, {}
        def counts(value):
            return {key: len(value.get(key) or []) for key in ('objects', 'relations', 'metrics')}
        return {'domain': domain, 'draft_version': draft.get('version'), 'draft_counts': counts(draft),
                'published_version': reference.version if reference else None,
                'published_hash': reference.content_hash if reference else None,
                'published_counts': counts(model)}
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.get("/api/v1/ontology/drafts/{domain}")
async def ontology_draft(domain: str) -> dict:
    try:
        return await ontology_service.get_draft(domain)
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.put("/api/v1/ontology/drafts/{domain}")
async def save_ontology_draft(domain: str, request: Request) -> dict:
    try:
        return await ontology_service.save_draft(domain, await request.json())
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


class KnowledgeBindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledgeBaseIds: list[str] = Field(max_length=20)


@app.patch("/api/v1/ontology/drafts/{domain}/knowledge-bases")
async def bind_knowledge_bases(domain: str, body: KnowledgeBindingInput):
    ids = list(dict.fromkeys(body.knowledgeBaseIds))
    try:
        if ids:
            accessible = {item["id"] for item in await knowledge_service.list_knowledge_bases()}
            if any(identifier not in accessible for identifier in ids):
                raise HTTPException(422, "所选知识库不在当前 WeKnora 账号可访问列表中，请刷新后重新选择")
        return await ontology_service.bind_knowledge_bases(domain, ids)
    except KnowledgeCoreError as exc:
        raise_knowledge_error(exc)
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.get("/api/v1/ontology/candidates", response_model=list[OntologyCandidate])
async def ontology_candidates(domain: str = "") -> list[dict]:
    try:
        return await ontology_service.list_candidates(domain)
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.post("/api/v1/ontology/candidates/{candidate_id}/decision", response_model=OntologyCandidate)
async def decide_ontology_candidate(candidate_id: str, request: Request) -> dict:
    body = await request.json()
    principal = getattr(request.state, "principal", None)
    reviewer = principal.username if principal is not None else "demo-data-steward"
    try:
        return await ontology_service.decide_candidate(
            candidate_id,
            body.get("decision", ""),
            reviewer=reviewer,
            note=str(body.get("note", ""))[:1000],
        )
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.post("/api/v1/ontology/publish")
async def publish_ontology(domain: str = Query(...,pattern=r"^[a-z][a-z0-9_]{1,99}$")) -> dict:
    try:
        draft = await ontology_service.get_draft(domain)
        sources = {str(o.get('dataSourceId') or draft.get('dataSourceId') or '') for o in draft.get('objects', [])}
        for source_id in sources:
            try:
                source = datasource_service.repository.get(source_id)
            except KeyError:
                raise HTTPException(422, f'本体数据源尚未登记：{source_id}')
            if source.status != 'healthy' or source.access != 'read_only':
                raise HTTPException(422, f'数据源尚未通过只读验证：{source.name}')
        return await ontology_service.publish(domain)
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.get("/api/v1/ontology/paths")
async def ontology_path(source: str, target: str, domain: str = Query(...,pattern=r"^[a-z][a-z0-9_]{1,99}$")) -> dict:
    try:
        return await ontology_service.find_path(domain, source, target)
    except OntologyCoreError as exc:
        raise_ontology_error(exc)


@app.get("/api/v1/datasources")
def datasources() -> list[dict]:
    return [item.public() for item in datasource_service.repository.list()]


@app.post("/api/v1/datasources", status_code=201)
async def create_datasource(body: DataSourceCreate) -> dict:
    if body.connection is None and body.secret_ref:
        item=datasource_service.repository.create(body.name, body.type, body.secret_ref)
        job=modeling_service.start(item.id,metadata_only=True)
        return {**item.public(),"onboarding_job_id":job["id"]}
    _verify_connection_receipt(body)
    identifier = str(uuid4())
    item = DataSourceRecord(id=identifier, name=body.name, type=body.type,
                            secret_ref=f"DATASOURCE_{identifier.replace('-', '').upper()}_URL")
    try:
        await datasource_service.executor.configuration(item, connection_payload(body))
        item=datasource_service.repository.save(item)
        job=modeling_service.start(item.id,metadata_only=True)
        return {**item.public(),"onboarding_job_id":job["id"]}
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.delete("/api/v1/datasources/{datasource_id}")
async def delete_datasource(datasource_id: str = Path(pattern=r"^[a-zA-Z0-9_-]{1,64}$")) -> dict[str, bool]:
    try:
        result = await ontology_service.delete_datasource(datasource_id)
    except OntologyCoreError as exc:
        if exc.status_code == 404:
            datasource_service.repository.evict(datasource_id)
        raise_ontology_error(exc)
    datasource_service.repository.evict(datasource_id)
    return result


@app.get("/api/v1/datasources/{datasource_id}/connection")
async def get_datasource_connection(datasource_id: str):
    try:
        return await datasource_service.executor.configuration(datasource_service.repository.get(datasource_id))
    except KeyError as exc:
        raise HTTPException(404, "数据源不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.put("/api/v1/datasources/{datasource_id}")
async def update_datasource(datasource_id: str, body: DataSourceCreate):
    try:
        datasource_service.repository.get(datasource_id)
    except KeyError as exc:
        raise HTTPException(404, "数据源不存在") from exc
    raise HTTPException(409, "数据源连接参数创建后不可修改；请新建数据源")


@app.patch("/api/v1/datasources/{datasource_id}")
async def rename_datasource(datasource_id: str, body: DataSourceRename):
    try:
        item = datasource_service.repository.get(datasource_id)
        item.name = body.name
        return datasource_service.repository.save(item).public()
    except KeyError as exc:
        raise HTTPException(404, "数据源不存在") from exc


@app.post("/api/v1/datasource-connection-test")
async def test_datasource_configuration(body: DataSourceCreate, datasource_id: str | None = None):
    try:
        item = (datasource_service.repository.get(datasource_id) if datasource_id else
                DataSourceRecord(id="connection-preview", name=body.name, type=body.type,
                                 secret_ref="DATASOURCE_CONNECTION_PREVIEW_URL"))
        if item.type != body.type:
            raise HTTPException(422, "数据库类型不一致")
        result = await datasource_service.executor.configuration(
            item, connection_payload(body), test=True
        )
        return {**result, "test_receipt": _issue_connection_receipt(body)}
    except KeyError as exc:
        raise HTTPException(404, "数据源不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/v1/datasources/{datasource_id}/test")
async def test_datasource(datasource_id: str) -> dict:
    try:
        result = await datasource_service.test(datasource_id)
        if result.get("read_only"):
            from .capability_service import schedule_validation
            from .database_routes import executor_request
            schedule_validation(datasource_service.repository.get(datasource_id), datasource_service.repository, executor_request)
        return result
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/v1/datasources/{datasource_id}/scan")
async def scan_datasource(datasource_id: str, tables: list[str] | None = Query(default=None, min_length=1, max_length=1000)) -> dict:
    try:
        return await datasource_service.scan(datasource_id, tables=tables) if tables is not None else await datasource_service.scan(datasource_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/v1/executions/{run_id}/cancel")
async def cancel_execution(run_id: str, request: Request) -> dict:
    require_run_access(run_id, request)
    try:
        return await workflow.cancel(run_id)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Executor 取消失败：{type(exc).__name__}") from exc
