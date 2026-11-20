"""Database extension APIs. Existing source IDs and synchronous scan remain compatible."""
import asyncio
from datetime import datetime, timezone
import io
import json
import zipfile
from uuid import uuid4
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from semora_contracts.database import PRODUCTS, native_protocol
from .datasources import datasource_service
from .mdl_export import build_mdl, merge_snapshot, column_type
from .datasource_table_storage import tables_digest
from .ontology import ontology_service
from .datasource_modeling import ModelingService, TERMINAL, _indexed, package_objects, package_relations, GENERATOR_VERSION, sampling_key
from .relation_output import clean_relations

router=APIRouter(prefix="/api/v1")
TASKS={}

def source(identifier):
    try:return datasource_service.repository.get(identifier)
    except KeyError:raise HTTPException(404,"数据源不存在") from None

async def executor_request(method, path, **kwargs):
    executor=datasource_service.executor
    try:
        async with httpx.AsyncClient(timeout=200) as client:
            response=await client.request(method,executor.base_url+"/internal/v1/"+path,
                headers={"X-Semora-Internal-Token":executor.token},**kwargs)
            if not response.is_success:
                try:detail=response.json().get("detail","Executor 请求失败")
                except ValueError:detail="Executor 请求失败"
                raise HTTPException(response.status_code,detail)
            return response.json()
    except httpx.HTTPError as exc:raise HTTPException(502,"Executor 请求失败："+type(exc).__name__) from exc

def payload(item):
    return {"datasource_id":item.id,"type":item.type,"secret_ref":item.secret_ref}

modeling_service=ModelingService(datasource_service,executor_request,ontology_service=ontology_service)

@router.get("/datasource-products")
def products():return [{"id":k,**v,"validation":"unverified"} for k,v in PRODUCTS.items()]

@router.get("/settings/database-drivers")
async def drivers():return await executor_request("GET","database-drivers")

@router.post("/settings/database-drivers",status_code=201)
async def install_driver(config: str=Form(...),files: list[UploadFile]=File(...)):
    data=[];total=0
    for file in files:
        content=await file.read(100*1024**2+1);total+=len(content)
        if total>100*1024**2:raise HTTPException(413,"驱动包超过 100MB")
        data.append(("files",(file.filename,content,"application/java-archive")))
    return await executor_request("POST","database-drivers",data={"config":config},files=data)

@router.put("/settings/database-drivers/{driver_id}")
async def update_driver(driver_id: str,body: dict):
    result=await executor_request("PUT",f"database-drivers/{driver_id}",json=body)
    # Connections referencing this immutable bundle need fresh validation.
    for item in datasource_service.repository.list():
        if not native_protocol(item.type):
            item.scan_result.pop("capability_cache", None)
            item.scan_result.get("connection", {}).pop("probes", None)
            item.scan_result.pop("capabilities",None);item.scan_result["scan_stale"]=True;item.status="unverified"
            datasource_service.repository.save(item)
    return result

@router.get("/datasources/{identifier}/capabilities")
def capabilities(identifier: str):
    item=source(identifier)
    from .database_profile import source_profile
    profile=source_profile(item)
    return {"connection":item.scan_result.get("connection",{}),
            "capabilities":item.public()["capabilities"] or {k:"pending" for k in ("connection","structure","comments","constraints","mdl","query")},
            "probe_cache":item.scan_result.get("connection",{}).get("probe_cache",{}),
            "features":profile["features"],"function_capabilities":profile["function_capabilities"]}

@router.get("/datasources/{identifier}/snapshot")
def snapshot(identifier: str):return source(identifier).scan_result

class ScanRequest(BaseModel):
    tables: list[str] | None=Field(default=None,min_length=1,max_length=1000)

@router.post("/datasources/{identifier}/scan-jobs",status_code=202)
async def start_scan(identifier: str,body: ScanRequest):
    item=source(identifier)
    old=item.scan_result.get("scan_job",{})
    if len(TASKS)>=2:raise HTTPException(429,"同时最多运行两个结构扫描")
    if old.get("id") in TASKS:raise HTTPException(409,"已有扫描任务正在运行")
    job={"id":uuid4().hex,"status":"running","phase":"metadata","completed_tables":0,
         "started_at":datetime.now(timezone.utc).isoformat()}
    item.scan_result={**item.scan_result,"scan_job":job};datasource_service.repository.save(item)
    async def work():
        try:
            result=await executor_request("POST","datasources/scan",json={**payload(item),"tables":body.tables,"execution_id":job["id"]})
            if job["status"]!="running":return
            # A connection edit invalidates the old task before it can publish stale metadata.
            if source(identifier).scan_result.get("scan_job",{}).get("id")!=job["id"]:return
            result['scan_job']=job
            datasource_service.repository.record_scan(identifier,result)
            job.update(status="completed",phase="complete",completed_tables=len(result.get("tables",[])))
        except asyncio.CancelledError:job.update(status="cancelled",phase="cancelled")
        except Exception as exc:
            job.update(status="failed",phase="failed",error=exc.detail if isinstance(exc,HTTPException) else type(exc).__name__)
        finally:
            if source(identifier).scan_result.get("scan_job",{}).get("id")==job["id"]:
                source(identifier).scan_result["scan_job"]=job;datasource_service.repository.save(source(identifier))
            TASKS.pop(job["id"],None)
    TASKS[job["id"]]=asyncio.create_task(work())
    from .relation_output import clean_relations
    return clean_relations(job)

@router.get("/datasources/{identifier}/scan-jobs/{job_id}")
async def scan_status(identifier: str,job_id: str):
    item=source(identifier);job=item.scan_result.get("scan_job",{})
    if job.get("id")!=job_id:raise HTTPException(404,"扫描任务不存在")
    if job.get("status")=="running" and job_id not in TASKS:
        job.update(status="interrupted",phase="interrupted");datasource_service.repository.save(item)
    if job.get("status")=="running":
        progress=await executor_request("GET",f"executions/{job_id}/progress")
        job.update(progress)
    from .relation_output import clean_relations
    return clean_relations(job)

@router.post("/datasources/{identifier}/scan-jobs/{job_id}/cancel")
async def cancel_scan(identifier: str,job_id: str):
    job=await scan_status(identifier,job_id)
    if job.get("status")=="running":
        job.update(status="cancelled",phase="cancelled")
        await executor_request("POST",f"executions/{job_id}/cancel")
        task=TASKS.get(job_id)
        if task:task.cancel()
        datasource_service.repository.save(source(identifier))
    from .relation_output import clean_relations
    return clean_relations(job)

class Decision(BaseModel):
    status: str=Field(pattern=r"^(accepted|rejected|pending)$")
    note: str=Field(default="",max_length=2000)


@router.get("/datasources/{identifier}/tables")
def datasource_tables(
    identifier: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    query: str = Query(default="", max_length=200),
):
    source(identifier)
    return datasource_service.repository.table_page(identifier, query, page, page_size)


@router.get("/datasources/{identifier}/tables/detail")
def datasource_table_detail(identifier: str, table_id: str = Query(min_length=1, max_length=512)):
    try:
        return datasource_service.repository.table_detail(identifier, table_id)
    except KeyError:
        raise HTTPException(404, "数据表不存在") from None


class RelationshipKeyPair(BaseModel):
    source: str = Field(min_length=1, max_length=255)
    target: str = Field(min_length=1, max_length=255)


class RelationshipEdit(BaseModel):
    source_table_id: str = Field(min_length=1, max_length=512)
    target_table_id: str = Field(min_length=1, max_length=512)
    key_pairs: list[RelationshipKeyPair] = Field(min_length=1, max_length=8)
    cardinality: Literal["1:1", "1:N", "N:1", "N:N", "unknown"]


class RelationshipDecision(BaseModel):
    action: Literal["approve", "reject", "disable", "enable"]


def _relationship_status(candidate):
    status = candidate.get("status")
    if status == "accepted":
        return "accepted"
    if status == "disabled":
        return "disabled"
    if status in {"pending", "unresolved"}:
        return "pending"
    return "excluded"


def _relationship_view(candidate):
    grade = candidate.get("evidence_grade")
    origin = "foreign_key" if grade == "foreign_key" else "manual" if grade == "manual" else "inferred"
    return {
        "id": candidate["id"],
        "source_table_id": candidate["source_table_id"],
        "target_table_id": candidate["target_table_id"],
        "source_columns": candidate["source_columns"],
        "target_columns": candidate["target_columns"],
        "key_pairs": candidate["key_pairs"],
        "cardinality": candidate["cardinality"],
        "status": _relationship_status(candidate),
        "origin": origin,
        "evidence_grade": grade,
        "review_note": candidate.get("review_note", ""),
        "decision_reason": next((e.get("reason") for e in reversed(candidate.get("evidence", [])) if e.get("kind") in {"profile_timeout", "automatic_exclusion", "independent_review", "model_semantic_review", "research_gap", "tool_unavailable", "profile_unavailable", "profile_audit_unavailable"}), ""),
        "predicates": candidate.get("condition_template", {}).get("guards", []),
        "automatically_excluded": candidate.get("status") == "rejected" and any(e.get("kind") == "automatic_exclusion" for e in candidate.get("evidence", [])),
    }


@router.get("/datasources/{identifier}/relationships")
def relations(
    identifier: str,
    status: str = Query(default="", pattern=r"^(|all|accepted|pending|disabled)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    source(identifier)
    records = modeling_service.repository.list_candidates(identifier, "single_source")
    visible = [item for item in records if _relationship_status(item) != "excluded"]
    counts = {key: sum(_relationship_status(item) == key for item in visible)
              for key in ("accepted", "pending", "disabled")}
    counts["all"] = len(visible)
    values = visible if status in {"", "all"} else [item for item in visible if _relationship_status(item) == status]
    values.sort(key=lambda item: ({"accepted": 0, "pending": 1, "disabled": 2}.get(_relationship_status(item), 3), -float(item.get("confidence") or 0), item["id"]))
    total = len(values)
    window = values[(page - 1) * page_size:page * page_size]
    return {"items": [_relationship_view(item) for item in window], "page": page, "page_size": page_size,
            "total": total, "counts": counts}


def _validated_manual_candidate(identifier: str, body: RelationshipEdit):
    if body.cardinality=='unknown':
        raise HTTPException(422,'请先选择明确的关联基数，再保存生效')
    item = source(identifier)
    tables = {table.get("id"): table for table in item.scan_result.get("tables") or []}
    left, right = tables.get(body.source_table_id), tables.get(body.target_table_id)
    if not left or not right:
        raise HTTPException(422, "关联关系引用了未扫描的数据表")
    left_columns = {column.get("name"): column for column in left.get("columns") or []}
    right_columns = {column.get("name"): column for column in right.get("columns") or []}
    pairs = [pair.model_dump() for pair in body.key_pairs]
    if len({(pair["source"], pair["target"]) for pair in pairs}) != len(pairs):
        raise HTTPException(422, "关联字段对不能重复")
    for pair in pairs:
        source_column, target_column = left_columns.get(pair["source"]), right_columns.get(pair["target"])
        if not source_column or not target_column:
            raise HTTPException(422, "关联关系引用了不存在的字段")
    schema_hash = tables_digest(list(tables.values()))
    try:
        package = modeling_service.repository.get_package(identifier)
    except KeyError:
        package = {"schema_hash": schema_hash, "status": "ready"}
    if package["schema_hash"] != schema_hash or package["status"] not in {"ready", "ready_with_pending"}:
        raise HTTPException(409, "单源本体正在更新，请完成后再编辑关系")
    return item, pairs, schema_hash


async def _save_source_relations(identifier, records):
    repository = modeling_service.repository
    item = source(identifier)
    schema_hash = tables_digest(item.scan_result.get("tables") or [])
    try:
        old = repository.get_package(identifier)
    except KeyError:
        old = {"payload": {"relations": []}}
    objects, mapping = package_objects(identifier, item.scan_result["tables"])
    records = clean_relations(records)
    relations = package_relations(records, mapping)
    payload = {"datasourceId": identifier, "schemaHash": schema_hash, "objects": objects,
               "relations": relations, "relationshipRecords": records}
    await modeling_service._commit_source_package(identifier,schema_hash,payload,item.scan_result['tables'])


async def _write_manual_relation(identifier, body, candidate_id=None):
    current = modeling_service.repository.latest_job(identifier)
    if current and current["status"] not in TERMINAL:
        raise HTTPException(409, "关联分析正在运行，请先取消任务再编辑")
    _, pairs, _ = _validated_manual_candidate(identifier, body)
    async with modeling_service.source_locks.setdefault(identifier, asyncio.Lock()):
        try:
            records = list(modeling_service.repository.get_package(identifier)["payload"].get("relationshipRecords", []))
        except KeyError:
            records = []
        previous = next((r for r in records if r["id"] == candidate_id), None)
        if candidate_id and previous is None:
            previous=next((r for r in modeling_service.repository.list_candidates(identifier,'single_source')
                           if r['id']==candidate_id and _relationship_status(r) in {'pending','disabled'}),None)
        if candidate_id and not previous:
            raise HTTPException(404, "关联关系不存在")
        from .query_relationships import validated_guards
        table_map={t['id']:t for t in source(identifier).scan_result['tables']}
        guard_record={'source_datasource_id':identifier,'target_datasource_id':identifier,
                      'source_table_id':body.source_table_id,'target_table_id':body.target_table_id}
        guards=validated_guards(guard_record,(previous or {}).get('condition_template',{}).get('guards',[]),
                                {identifier:table_map})
        if guards is None:
            raise HTTPException(422,'编辑后的关联表与原附加条件不兼容')
        record = {**(previous or {}), "id": candidate_id or uuid4().hex,
            "scope": "single_source", "source_datasource_id": identifier, "target_datasource_id": identifier,
            "source_table_id": body.source_table_id, "target_table_id": body.target_table_id,
            "source_columns": [p["source"] for p in pairs], "target_columns": [p["target"] for p in pairs],
            "key_pairs": pairs, "cardinality": body.cardinality, "status": "accepted",
            "evidence_grade": "manual", "reviewed_by": "data-steward",
            "research_pending": False, "review_required": False, "review_reasons": [],
            "condition_template": {"operator": "and_equals", "guards": guards},
            "evidence": [{"kind": "manual", "reason": "用户配置"}]}
        await _save_source_relations(identifier, [r for r in records if r["id"] != candidate_id] + [record])
        schema_hash = tables_digest(source(identifier).scan_result.get("tables") or [])
        modeling_service.repository.replace_candidates([record], schema_hash, schema_hash)
        return _relationship_view(record)


@router.post("/datasources/{identifier}/relationships", status_code=201)
async def add_relationship(identifier: str, body: RelationshipEdit):
    return await _write_manual_relation(identifier, body)


@router.put("/datasources/{identifier}/relationships/{candidate_id}")
async def edit_relationship(identifier: str, candidate_id: str, body: RelationshipEdit):
    return await _write_manual_relation(identifier, body, candidate_id)


async def _decide_relationship(identifier: str, candidate_id: str, action: str):
    current = modeling_service.repository.latest_job(identifier)
    if current and current["status"] not in TERMINAL:
        raise HTTPException(409, "关联分析正在运行，请先取消任务再审核")
    item = source(identifier)
    async with modeling_service.source_locks.setdefault(identifier, asyncio.Lock()):
        records = modeling_service.repository.list_candidates(identifier, "single_source")
        previous = next((record for record in records if record["id"] == candidate_id), None)
        if not previous or _relationship_status(previous) == "excluded":
            raise HTTPException(404, "关联关系不存在")
        expected = {"approve": "pending", "reject": "pending", "disable": "accepted", "enable": "disabled"}[action]
        if _relationship_status(previous) != expected:
            raise HTTPException(409, "当前关系状态不允许此操作")
        if action in {'approve','enable'} and previous.get('cardinality') not in {'1:1','1:N','N:1','N:N'}:
            raise HTTPException(409,'关联基数未确定，请先编辑并选择明确基数后保存生效')
        schema_hash = tables_digest(item.scan_result.get("tables") or [])
        if previous.get("source_schema_hash") not in {None, schema_hash} or previous.get("target_schema_hash") not in {None, schema_hash}:
            raise HTTPException(409, "结构已变化，请先更新关联关系")
        labels = {"approve": "人工同意", "reject": "人工拒绝", "disable": "人工停用", "enable": "人工启用"}
        status = "accepted" if action in {"approve", "enable"} else "disabled"
        record = {**previous, "status": status, "research_pending": False,
                  "reviewed_by": "data-steward", "review_note": labels[action],
                  "evidence": [*previous.get("evidence", []), {"kind": "manual_review", "action": action, "reason": labels[action]}]}
        modeling_service.repository.replace_candidates([record], schema_hash, schema_hash)
        active = [value for value in modeling_service.repository.list_candidates(identifier, "single_source")
                  if value.get("status") == "accepted"]
        await _save_source_relations(identifier, active)
        return _relationship_view(record)


@router.post("/datasources/{identifier}/relationships/{candidate_id}/decision")
async def decide_relationship(identifier: str, candidate_id: str, body: RelationshipDecision):
    return await _decide_relationship(identifier, candidate_id, body.action)


@router.delete("/datasources/{identifier}/relationships/{candidate_id}")
async def delete_relationship(identifier: str, candidate_id: str):
    # Backward-compatible route: relations are disabled, never physically deleted.
    value = await _decide_relationship(identifier, candidate_id, "disable")
    return {"id": candidate_id, "deleted": False, "disabled": True, "status": value["status"]}


@router.post("/datasources/{identifier}/relationships/{candidate_id}/restore")
async def restore_relationship(identifier: str, candidate_id: str):
    return await _decide_relationship(identifier, candidate_id, "enable")


@router.get("/datasources/{identifier}/mdl-report")
def mdl_report(identifier: str):
    item=source(identifier);_,report=build_mdl(item.scan_result,item.type)
    return report

@router.get("/datasources/{identifier}/mdl")
def download_mdl(identifier: str):
    item=source(identifier);mdl,report=build_mdl(item.scan_result,item.type)
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
        if mdl is not None:archive.writestr("mdl.json",json.dumps(mdl,ensure_ascii=False,sort_keys=True,indent=2))
        archive.writestr("report.json",json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2))
    item.scan_result.setdefault("capabilities",{})["mdl"]="verified" if report["status"]=="passed" else "partial" if mdl else "failed"
    datasource_service.repository.save(item)
    return Response(output.getvalue(),media_type="application/zip",headers={"Content-Disposition":'attachment; filename="semora-mdl.zip"'})


@router.post("/datasources/{identifier}/validate-query")
async def validate_query(identifier: str, force: bool = False):
    item=source(identifier)
    from .capability_service import validate_cached, TASKS as probe_tasks
    try:
        pending = probe_tasks.get(identifier)
        result = await asyncio.shield(pending) if pending else None
        if result is None or force:
            result=await validate_cached(item, datasource_service.repository, executor_request, force=force)
    except HTTPException:
        item.status="unverified"
        item.scan_result.setdefault("capabilities",{})["query"]="failed"
        datasource_service.repository.save(item)
        raise
    return result


@router.post("/datasources/{identifier}/relationships/{candidate_id}/profile")
async def profile(identifier: str,candidate_id: str):
    item=source(identifier)
    candidate=next((r for r in item.scan_result.get("relationship_candidates",[]) if r["id"]==candidate_id),None)
    if not candidate:raise HTTPException(404,"候选不存在")
    table_map={table.get("id"):table for table in item.scan_result.get("tables",[])}
    source_table=table_map.get(candidate["source_table"],{})
    target_table=table_map.get(candidate["target_table"],{})
    if not _indexed(source_table,candidate["columns"]) or not _indexed(target_table,candidate["target_columns"],unique=True):
        raise HTTPException(422,"该候选没有满足安全采样要求的来源索引和目标唯一索引")
    result=await executor_request("POST","datasources/profile-relationship",json={"datasource":payload(item),
        "source_table":candidate["source_table"],"source_columns":candidate["columns"],
        "target_table":candidate["target_table"],"target_columns":candidate["target_columns"],"sample_rows":100,
        "source_index_confirmed":True,"target_unique_index_confirmed":True,
        "source_sampling_key":sampling_key(source_table),
        "target_sampling_key":sampling_key(target_table)})
    candidate["profile"]=result;datasource_service.repository.save(item)
    return result


class OntologyRelationInput(BaseModel):
    domain: str=Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    source_object: str=Field(min_length=1,max_length=200)
    target_object: str=Field(min_length=1,max_length=200)

@router.post("/datasources/{identifier}/relationships/{candidate_id}/ontology-candidate")
async def ontology_candidate(identifier: str,candidate_id: str,body: OntologyRelationInput):
    raise HTTPException(410, "请直接在业务域本体配置中添加关联关系")


@router.get("/datasources/{identifier}/onboarding")
def onboarding_status(identifier: str, relationships_only: bool = False):
    source(identifier)
    job=modeling_service.repository.latest_job(identifier, relationships_only=relationships_only)
    if not job:raise HTTPException(404,"数据源尚未开始自动建模")
    return {**job, "details": {key: value for key, value in job.get("details", {}).items()
                               if key not in {"query_table_roles", "pending"}}}


@router.post("/datasources/{identifier}/onboarding/retry",status_code=202)
async def retry_onboarding(identifier: str):
    source(identifier)
    previous=modeling_service.repository.latest_job(identifier)
    return modeling_service.start(identifier,force=False,metadata_only=bool((previous or {}).get('details',{}).get('metadata_only',True)))


@router.post("/datasources/{identifier}/onboarding/{job_id}/resume", status_code=202)
async def resume_failed_analysis(identifier: str, job_id: str):
    source(identifier)
    try:
        return modeling_service.resume_failed(identifier, job_id)
    except KeyError:
        raise HTTPException(404, '分析任务不存在') from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/datasources/{identifier}/onboarding/{job_id}/cancel")
async def cancel_onboarding(identifier: str, job_id: str):
    source(identifier)
    try:
        return await modeling_service.cancel(identifier, job_id)
    except KeyError:
        raise HTTPException(404, "分析任务不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/datasources/{identifier}/relationships/analyze", status_code=202)
async def analyze_relationships(identifier: str):
    item = source(identifier)
    if 'tables' not in item.scan_result:
        raise HTTPException(409, "尚无已扫描结构，请先重新扫描")
    current=modeling_service.repository.latest_job(identifier)
    if current and current['status'] not in TERMINAL:
        raise HTTPException(409,'任务正在运行，不能重新分析')
    return modeling_service.start(identifier, relations_only=True, fresh=True)


class RelationshipStageInput(BaseModel):
    stage:Literal['keywords','retrieval','organization','discovery']
    job_id:str|None=None


@router.post('/datasources/{identifier}/relationships/analysis-stages', status_code=202)
async def run_relationship_stage(identifier:str, body:RelationshipStageInput):
    source(identifier)
    try:
        return modeling_service.start_relationship_stage(identifier,body.stage,body.job_id)
    except KeyError:
        raise HTTPException(404,'阶段任务不存在') from None
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.get('/datasources/{identifier}/relationships/analysis-stages/{job_id}')
def relationship_stage_result(identifier:str,job_id:str,section:Literal['keywords','retrieval','organization','proposals']='keywords',
                              keyword:str|None=None,page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=200)):
    source(identifier)
    try:
        job=modeling_service.repository.get_job(job_id)
        if job['datasource_id']!=identifier or not job.get('details',{}).get('stage_only'):
            raise KeyError(job_id)
        from .relationship_work import RelationshipWork,job_input_hash,normalize_keyword
        from .relationship_inputs import digest
        repository=modeling_service.repository
        previous=job_input_hash(repository,job)
        engine=None
        if previous and repository.dsn:
            work=RelationshipWork(repository,job_id,previous,{},lambda:None)
        else:
            engine=modeling_service.relationship_stage_context(job)
            work=engine.work
        def stage_result(stage):
            record=work.get('stage',digest(stage))
            return record['payload'] if record and record['status']=='completed' else {}
        if section=='keywords': values=work.keywords()
        elif section=='retrieval':
            # A read endpoint must not perform retrieval or create work records.
            if keyword is None:
                values=[{'keyword':k,'total':v} for k,v in stage_result('retrieval').get('counts',{}).items()]
            else:
                found=work.get('retrieval',digest(normalize_keyword(keyword)))
                values=found['payload']['items'] if found and found['status']=='completed' else []
                try:
                    engine=engine or modeling_service.relationship_stage_context(job)
                except ValueError:
                    pass  # Historical hits remain readable without substituting current metadata.
                if engine is not None: values=[engine.brief(v) for v in values]
        elif section=='organization':
            organized=stage_result('organization')
            values=[{'kind':'group',**g} for g in organized.get('groups',[])]+[
                {'kind':'excluded',**h} for h in organized.get('excluded',[])]
        else: values=stage_result('discovery').get('proposals',[])
        return {'job':job,'section':section,'items':values[(page-1)*page_size:page*page_size],
                'page':page,'page_size':page_size,'total':len(values)}
    except KeyError:
        raise HTTPException(404,'阶段任务不存在') from None
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.post("/datasources/{identifier}/rescan",status_code=202)
async def rescan(identifier: str):
    source(identifier)
    return modeling_service.start(identifier,force=True,metadata_only=True)


@router.get("/datasources/{identifier}/source-ontology")
def source_ontology(identifier: str):
    source(identifier)
    try:package=modeling_service.repository.get_package(identifier)
    except KeyError:raise HTTPException(404,"数据源尚未生成单源本体包") from None
    package["candidates"]=[c for c in modeling_service.repository.list_candidates(identifier,"single_source") if c["status"] == "accepted"]
    from .relation_output import clean_relations
    return clean_relations(package)


@router.put("/relationship-candidates/{candidate_id}")
async def decide_discovered_candidate(candidate_id: str, body: Decision):
    raise HTTPException(410, "关联生成后自动生效，不再提供候选确认接口")


@router.post("/ontology/configurations/{domain}/sources/{identifier}")
async def bind_source_package(domain: str,identifier: str):
    item=source(identifier)
    try:package=modeling_service.repository.get_package(identifier)
    except KeyError:raise HTTPException(409,"请先手动更新关联关系") from None
    from .datasource_table_storage import tables_digest
    tables=item.scan_result.get("tables") or []
    if (item.status!="healthy" or not tables or package["schema_hash"]!=tables_digest(tables)
            or package["status"] not in {"ready","ready_with_pending"}):
        raise HTTPException(409,"请先手动更新关联关系，使其与当前结构一致")
    try:configuration=await ontology_service.get_configuration(domain)
    except Exception as exc:raise HTTPException(404,"业务域不存在") from exc
    refs={ref["dataSourceId"]:ref for ref in configuration.get("sourcePackageRefs",[]) if ref.get("dataSourceId")}
    refs[identifier]={"dataSourceId":identifier,"schemaHash":package["schema_hash"]}
    configuration['sourcePackageRefs']=list(refs.values())
    configuration["dataSourceId"]=configuration.get("dataSourceId") or item.id
    return await ontology_service.save_configuration(domain,configuration)


@router.delete('/ontology/configurations/{domain}/sources/{identifier}')
async def unbind_source_package(domain:str,identifier:str):
    if any(j.get('domain')==domain and identifier in {j.get('datasource_id'),j.get('peer_datasource_id')}
           for j in modeling_service.repository.interrupted_jobs()):
        raise HTTPException(409,'当前数据源正在进行跨源分析，请完成或取消后再解除绑定')
    result=await ontology_service.unbind_source(domain,identifier)
    for job in modeling_service.repository.memory_jobs.values():
        if (job.get('kind') == 'cross_source' and job.get('domain') == domain
                and identifier in {job['datasource_id'], job['peer_datasource_id']}):
            job.setdefault('details', {})['pair_unbound'] = True
    modeling_service.repository.memory_candidates={key:value for key,value in modeling_service.repository.memory_candidates.items()
        if not (value.get('scope')=='cross_source' and value.get('domain')==domain
                and identifier in {value.get('source_datasource_id'),value.get('target_datasource_id')})}
    return result


@router.get('/ontology/configurations/{domain}/relationships')
async def domain_relationships(domain:str):
    from .domain_relationships import review_rows
    configuration=await ontology_service.get_configuration(domain)
    rows=review_rows(configuration,domain,modeling_service)
    return clean_relations({'items':rows,'counts':{status:sum(r['status']==status for r in rows) for status in ('accepted','pending','disabled')}})


def ensure_domain_idle(domain):
    if any(j.get('domain')==domain for j in modeling_service.repository.interrupted_jobs()):
        raise HTTPException(409,'本业务域正在分析关联关系，请完成后再编辑')


@router.put('/ontology/configurations/{domain}/relationships/{identifier}')
async def edit_domain_relationship(domain:str,identifier:str,body:dict):
    from .domain_relationships import review_rows,save_domain_relation
    ensure_domain_idle(domain)
    configuration=await ontology_service.get_configuration(domain)
    if not any(r['id']==identifier for r in review_rows(configuration,domain,modeling_service)):
        raise HTTPException(404,'跨库关联关系不存在')
    return await save_domain_relation(domain,identifier,body,configuration,modeling_service,ontology_service)


@router.post('/ontology/configurations/{domain}/relationships/{identifier}/decision')
async def decide_domain_relationship(domain:str,identifier:str,body:RelationshipDecision):
    from .domain_relationships import review_rows,records_for_domain,save_domain_relation,manual_record,KNOWN_CARDINALITIES
    ensure_domain_idle(domain)
    configuration=await ontology_service.get_configuration(domain)
    row=next((r for r in review_rows(configuration,domain,modeling_service) if r['id']==identifier),None)
    if row is None:raise HTTPException(404,'跨库关联关系不存在')
    expected={'approve':'pending','reject':'pending','disable':'accepted','enable':'disabled'}[body.action]
    if row['status']!=expected:raise HTTPException(409,'当前关系状态不允许此操作')
    if body.action in {'approve','enable'}:
        if row['relation'].get('cardinality') not in KNOWN_CARDINALITIES:
            raise HTTPException(409,'关联基数未确定，请先编辑并选择明确基数')
        return await save_domain_relation(domain,identifier,row['relation'],configuration,modeling_service,ontology_service)
    record=records_for_domain(configuration,domain,modeling_service).get(identifier)
    if record is None:record=manual_record(row['relation'],configuration,domain,modeling_service,identifier)
    configuration['relations']=[r for r in configuration.get('relations',[]) if r.get('metadata',{}).get('candidateId')!=identifier
                                and r['name']!=row['relation']['name']]
    result=await ontology_service.save_configuration(domain,configuration)
    record={**record,'status':'disabled','reviewed_by':'data-steward','research_pending':False,
            'review_note':'人工拒绝' if body.action=='reject' else '人工停用'}
    hashes=[tables_digest(modeling_service.datasources.repository.get(record[side+'_datasource_id']).scan_result['tables']) for side in ('source','target')]
    modeling_service.repository.replace_candidates([record],*hashes)
    return result


class CrossSourceInput(BaseModel):
    left_source_id: str=Field(min_length=1,max_length=64)
    right_source_id: str=Field(min_length=1,max_length=64)
    mode: Literal["create", "reanalyze"] = "create"


@router.get('/ontology/configurations/{domain}/cross-source-analysis')
async def list_cross_source_analysis(domain:str):
    configuration=await ontology_service.get_configuration(domain)
    bound={ref['dataSourceId'] for ref in configuration.get('sourcePackageRefs',[])}
    bound.update(obj.get('dataSourceId') or configuration.get('dataSourceId') for obj in configuration.get('objects',[]))
    return clean_relations({'items':[{k:v for k,v in job.items() if k!='candidates'}
        for job in modeling_service.repository.domain_cross_jobs(domain)
        if job['datasource_id'] in bound and job.get('peer_datasource_id') in bound]})


@router.post("/ontology/configurations/{domain}/cross-source-analysis",status_code=202)
async def start_cross_source(domain: str,body: CrossSourceInput):
    try:configuration=await ontology_service.get_configuration(domain)
    except Exception as exc:raise HTTPException(404,"业务域不存在") from exc
    bound={ref.get("dataSourceId") for ref in configuration.get("sourcePackageRefs",[])}
    bound.update(obj.get("dataSourceId") or configuration.get("dataSourceId") for obj in configuration.get("objects",[]))
    if body.left_source_id not in bound or body.right_source_id not in bound:
        raise HTTPException(422,"两个数据源必须先绑定到当前业务域")
    source_pair = {body.left_source_id, body.right_source_id}
    if len(source_pair) != 2:
        raise HTTPException(422, "请选择两个不同的数据源")
    from .domain_relationships import objects_by_name, review_rows

    pair_exists = any(
        {job["datasource_id"], job["peer_datasource_id"]} == source_pair
        for job in modeling_service.repository.domain_cross_jobs(domain)
    )
    if not pair_exists:
        objects = objects_by_name(configuration)
        pair_exists = any(
            {objects.get(row["relation"].get(side), {}).get("dataSourceId") for side in ("from", "to")}
            == source_pair
            for row in review_rows(configuration, domain, modeling_service)
        )
    if pair_exists and body.mode == "create":
        raise HTTPException(409, "这两个数据源已添加关联，请在已有卡片中重新分析。")
    if not pair_exists and body.mode == "reanalyze":
        raise HTTPException(409, "这两个数据源尚未添加关联，请先新增关联。")
    try:return modeling_service.start_cross_source(domain,body.left_source_id,body.right_source_id)
    except (KeyError,ValueError) as exc:raise HTTPException(409,str(exc)) from exc


@router.get("/ontology/configurations/{domain}/cross-source-analysis/{job_id}")
async def cross_source_status(domain: str,job_id: str):
    try:job=modeling_service.repository.get_job(job_id)
    except KeyError:raise HTTPException(404,"跨源分析任务不存在") from None
    if job.get("domain")!=domain or job.get("kind")!="cross_source":raise HTTPException(404,"跨源分析任务不存在")
    if job["status"] in TERMINAL:
        configuration = await ontology_service.get_configuration(domain)
        job["candidates"] = [r.get("metadata", {}).get("relationshipRecord") for r in configuration.get("relations", [])
            if r.get("metadata", {}).get("relationshipRecord", {}).get("scope") == "cross_source"
            and {r["metadata"]["relationshipRecord"]["source_datasource_id"], r["metadata"]["relationshipRecord"]["target_datasource_id"]}
            == {job["datasource_id"], job["peer_datasource_id"]}]
    return clean_relations({**job, "details": {k: v for k, v in job.get("details", {}).items() if k not in {"query_table_roles", "pending"}}})


@router.post('/ontology/configurations/{domain}/cross-source-analysis/{job_id}/cancel')
async def cancel_cross_source_analysis(domain:str,job_id:str):
    try:
        job=modeling_service.repository.get_job(job_id)
        if job.get('domain')!=domain or job.get('kind')!='cross_source':raise KeyError(job_id)
        return clean_relations(await modeling_service.cancel(job['datasource_id'],job_id,kind='cross_source'))
    except KeyError:raise HTTPException(404,'跨源分析任务不存在') from None
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
