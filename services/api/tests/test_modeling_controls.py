import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import database_routes, main
from app.datasource_modeling import ModelingRepository, ModelingService
from app.datasources import DataSourceRecord, DataSourceRepository, DataSourceService


def setup_service():
    sources=DataSourceRepository(dsn='')
    source=DataSourceRecord('source','Source','postgresql','NO_CREDENTIAL',status='healthy')
    source.scan_result['tables']=[{'id':'s.t','name':'t','columns':[{'name':'id','type':'bigint'},{'name':'entity_ref','type':'bigint'}],
                                  'primary_keys':[{'columns':['id']}]}]
    sources.save(source)
    repo=ModelingRepository(dsn='')
    service=ModelingService(SimpleNamespace(repository=sources), None, repository=repo)
    return service,repo,sources


def test_job_progress_never_regresses_through_phases_or_retries():
    service,repo,_=setup_service()
    job=repo.create_job('source')
    observed=[]
    for phase,value in [('extracting_keywords',48),('organizing_recall_groups',50),
                        ('comparing',55),('comparing',52),('researching',98),
                        ('auditing_evidence',98),('researching',70)]:
        service._update(job,'profiling',phase,value)
        observed.append(job['progress'])
    assert observed==sorted(observed)
    service._update(job,'building','publishing',100)
    assert job['progress']==99
    service._update(job,'completed','complete',100)
    assert job['progress']==100


def test_resuming_terminal_job_starts_new_progress_but_keeps_it_monotonic():
    service,repo,_=setup_service()
    job=repo.create_job('source',details={'checkpoint_ref':'saved','technical_incomplete':True,
        'stage_done':87,'stage_total':87,'stage_failed':52})
    service._update(job,'completed_with_pending','incomplete',100)
    async def continuation(value,**kwargs):
        assert value['progress']==0 and 'stage_done' not in value['details']
        service._update(value,'profiling','comparing',54)
        service._update(value,'profiling','comparing',52)
        assert value['progress']==54
        service._update(value,'building','publishing',100)
        assert value['progress']==99
        service._update(value,'completed','complete',100)
    service._run_onboarding=continuation
    async def run():
        resumed=service.resume_failed('source',job['id'])
        await service.tasks[job['id']]
        assert resumed['progress']==100 and resumed['details']['resume_attempt']==1
    asyncio.run(run())


def test_partial_cross_job_resume_uses_cross_runner_only():
    service,repo,_=setup_service();called=[]
    job=repo.create_job('source',kind='cross_source',peer_datasource_id='peer',domain='demo',
                        details={'checkpoint_ref':'saved-cross'})
    job['status']='completed_with_pending';repo.save_job(job)
    async def cross(value):called.append(value['id'])
    service._run_cross_source=cross
    async def run():
        restored=service.resume_failed('source',job['id'])
        await service.tasks[job['id']]
        assert restored['details']['resume_research'] is True
    asyncio.run(run())
    assert called==[job['id']]


def test_partial_single_source_job_keeps_effective_package_and_can_resume(monkeypatch):
    service,repo,_=setup_service()
    original=repo.save_package('source','old',{'objects':[],'relations':[{'name':'existing'}]},'ready',0)
    async def review(candidates,maps,job,*args,**kwargs):
        job['details'].update(technical_incomplete=True,incomplete_count=1,
            incomplete_stages=['analyzing_large_groups'],checkpoint_ref=job['id'])
    monkeypatch.setattr(service,'_semantic_review',review)
    async def run():
        job=service.start('source',relations_only=True,fresh=True)
        await service.tasks[job['id']]
        assert job['status']=='completed_with_pending' and job['phase']=='incomplete'
        assert job['details']['relationships_updated'] is False
        assert repo.get_package('source')==original
        resumed=[]
        async def continuation(value,**kwargs):resumed.append(value['id'])
        monkeypatch.setattr(service,'_run_onboarding',continuation)
        service.resume_failed('source',job['id'])
        await service.tasks[job['id']]
        assert resumed==[job['id']]
    asyncio.run(run())


def test_failed_keyword_stage_is_resumable_and_does_not_publish(monkeypatch,tmp_path):
    from semora_agent.model import structured_model
    service,repo,_=setup_service()
    original=repo.save_package('source','old',{'objects':[],'relations':[{'name':'existing'}]},'ready',0)
    monkeypatch.setenv('RELATIONSHIP_CHECKPOINT_DIR',str(tmp_path))
    broken=True
    async def model(system,user):
        if broken:raise TimeoutError('temporary model timeout')
        return {'keywords':['entity_ref']}
    monkeypatch.setattr(structured_model,'complete_json',model)
    async def run():
        nonlocal broken
        job=service.start_relationship_stage('source','keywords')
        await service.tasks[job['id']]
        assert job['status']=='completed_with_pending' and job['details']['incomplete_count']==1
        assert repo.get_package('source')==original
        broken=False
        resumed=service.resume_failed('source',job['id'])
        await service.tasks[job['id']]
        assert resumed['status']=='completed' and not resumed['details']['technical_incomplete']
        assert repo.get_package('source')==original
    asyncio.run(run())


def test_keyword_step_stops_before_retrieval_and_never_publishes(monkeypatch,tmp_path):
    from semora_agent.model import structured_model
    service,repo,_=setup_service()
    original=repo.save_package('source','old',{'objects':[],'relations':[{'name':'existing'}]},'ready',0)
    calls=[]
    async def model(system,user):
        import json
        payload=json.loads(user); calls.append(payload['phase'])
        assert payload['phase']=='keywords'
        return {'keywords':['entity_ref']}
    monkeypatch.setattr(structured_model,'complete_json',model)
    monkeypatch.setenv('RELATIONSHIP_CHECKPOINT_DIR',str(tmp_path))
    async def run():
        job=service.start_relationship_stage('source','keywords')
        await service.tasks[job['id']]
        assert job['status']=='completed' and job['phase']=='keywords_complete'
        assert repo.get_package('source')==original
        continued=service.start_relationship_stage('source','retrieval',job['id'])
        await service.tasks[continued['id']]
        assert continued['status']=='completed'
        assert calls==['keywords']
        assert repo.get_package('source')==original
    asyncio.run(run())


def test_new_step_cannot_skip_keyword_stage():
    service,_,_=setup_service()
    with pytest.raises(ValueError,match='先执行关键词'):
        service.start_relationship_stage('source','retrieval')


def test_historical_keywords_read_original_input_namespace_after_prompt_update(monkeypatch):
    from fastapi import FastAPI
    service,repo,sources=setup_service()
    job=repo.create_job('source',details={'stage_only':True,'analysis_input_hash':'previous-input'})
    job['status']='completed'
    repo.dsn='not-used-by-this-read'
    class StoredWork:
        def __init__(self,repository,job_id,input_hash,*args):
            assert input_hash=='previous-input'
        def keywords(self):return ['previous_keyword']
    def forbidden(*args):raise AssertionError('historical reads must not rebuild current model input')
    monkeypatch.setattr('app.relationship_work.RelationshipWork',StoredWork)
    monkeypatch.setattr(service,'relationship_stage_context',forbidden)
    monkeypatch.setattr(database_routes,'modeling_service',service)
    monkeypatch.setattr(database_routes,'datasource_service',SimpleNamespace(repository=sources))
    app=FastAPI();app.include_router(database_routes.router)
    response=TestClient(app).get(f'/api/v1/datasources/source/relationships/analysis-stages/{job["id"]}')
    assert response.status_code==200 and response.json()['items']==['previous_keyword']


def test_stage_http_inputs_outputs_and_read_endpoint_are_side_effect_free(monkeypatch,tmp_path):
    from fastapi import FastAPI
    from semora_agent.model import structured_model
    service,repo,sources=setup_service(); calls=[]
    async def model(system,user):
        calls.append(user)
        return {'keywords':['entity_ref']}
    monkeypatch.setattr(structured_model,'complete_json',model)
    monkeypatch.setenv('RELATIONSHIP_CHECKPOINT_DIR',str(tmp_path))
    monkeypatch.setattr(database_routes,'modeling_service',service)
    monkeypatch.setattr(database_routes,'datasource_service',SimpleNamespace(repository=sources))
    app=FastAPI(); app.include_router(database_routes.router)
    base='/api/v1/datasources/source/relationships/analysis-stages'
    with TestClient(app) as client:
        response=client.post(base,json={'stage':'keywords'})
        assert response.status_code==202
        job_id=response.json()['id']
        for _ in range(10):
            view=client.get(base+'/'+job_id).json()
            if view['job']['status']=='completed': break
        assert view['job']['phase']=='keywords_complete'
        assert view['items']==['entity_ref'] and view['total']==1
        before=service.relationship_stage_context(repo.get_job(job_id)).state.copy()
        empty=client.get(base+'/'+job_id,params={'section':'retrieval','keyword':'entity_ref'})
        assert empty.status_code==200 and empty.json()['items']==[]
        assert service.relationship_stage_context(repo.get_job(job_id)).state==before
        assert len(calls)==1
        assert client.post(base,json={'stage':'publish'}).status_code==422
        assert client.get(base+'/missing').status_code==404


def test_organization_stage_is_explicit_readable_and_never_publishes(monkeypatch,tmp_path):
    import json
    from fastapi import FastAPI
    from semora_agent.model import structured_model
    service,repo,sources=setup_service();calls=[]
    item=sources.get('source')
    item.scan_result['tables']=[{'id':f's.t{i}','name':f't{i}','columns':[
        {'name':'object_ref','type':'bigint','comment':'对象编号'}]} for i in range(2)]
    sources.save(item)
    original=repo.save_package('source','old',{'objects':[],'relations':[{'name':'existing'}]},'ready',0)
    async def model(system,user):
        p=json.loads(user);calls.append(p['phase'])
        if p['phase']=='keywords':return {'keywords':['object_ref']}
        assert p['phase']=='organization'
        return {'groups':[{'table_ids':g['table_ids'],
                           'reason':'未发现编号体系冲突，保留待分析'} for g in p['groups']], 'excluded':[]}
    monkeypatch.setattr('app.relationship_organization.ORGANIZATION_GROUP_LIMIT',1)
    monkeypatch.setattr(structured_model,'complete_json',model)
    monkeypatch.setenv('RELATIONSHIP_CHECKPOINT_DIR',str(tmp_path))
    async def run():
        job=service.start_relationship_stage('source','keywords')
        await service.tasks[job['id']]
        for phase in ['retrieval','organization']:
            job=service.start_relationship_stage('source',phase,job['id'])
            await service.tasks[job['id']]
            assert job['status']=='completed'
        assert job['phase']=='organization_complete'
        assert job['details']['organization']['output_groups']==1
        assert calls==['keywords','organization']
        assert repo.get_package('source')==original
        return job
    job=asyncio.run(run())
    monkeypatch.setattr(database_routes,'modeling_service',service)
    monkeypatch.setattr(database_routes,'datasource_service',SimpleNamespace(repository=sources))
    app=FastAPI();app.include_router(database_routes.router)
    response=TestClient(app).get(f'/api/v1/datasources/source/relationships/analysis-stages/{job["id"]}',
                               params={'section':'organization'})
    assert response.status_code==200
    assert response.json()['items'][0]['kind']=='group'
    assert len(response.json()['items'][0]['table_ids'])==2
    assert calls==['keywords','organization']


def test_cancel_live_research_stops_task_and_keeps_effective_package(monkeypatch):
    service,repo,_=setup_service()
    original=repo.save_package('source','old',{'objects':[],'relations':[{'name':'existing'}]},'ready',0)
    started=asyncio.Event()
    stopped=asyncio.Event()
    async def review(*args,**kwargs):
        started.set()
        try:await asyncio.Event().wait()
        finally:stopped.set()
    monkeypatch.setattr(service,'_semantic_review',review)
    async def run():
        job=service.start('source',relations_only=True,fresh=True)
        await asyncio.wait_for(started.wait(),2)
        task=service.tasks[job['id']]
        cancelled=await service.cancel('source',job['id'])
        assert cancelled['status']=='cancelled'
        assert cancelled['completed_at']
        assert task.done() and stopped.is_set()
        assert service.tasks=={}
        assert repo.get_package('source')==original
        assert (await service.cancel('source',job['id']))['status']=='cancelled'
    asyncio.run(run())


def test_cancel_cross_research_stops_model_task_without_publishing(monkeypatch):
    from copy import deepcopy
    from app.datasource_modeling import tables_digest
    service,repo,sources=setup_service()
    peer=deepcopy(sources.get('source'));peer.id='peer';sources.save(peer)
    for sid in ('source','peer'):
        tables=sources.get(sid).scan_result['tables']
        repo.save_package(sid,tables_digest(tables),{'objects':[],'relations':[{'name':'existing'}]},'ready',0)
    started=asyncio.Event();stopped=asyncio.Event();publications=[]
    async def review(*args,**kwargs):
        started.set()
        try:await asyncio.Event().wait()
        finally:stopped.set()
    async def publish(*args,**kwargs):publications.append(args)
    monkeypatch.setattr(service,'_semantic_review',review)
    monkeypatch.setattr(service,'_apply_cross_relations',publish)
    service.ontology=SimpleNamespace()
    async def run():
        job=service.start_cross_source('demo','source','peer')
        await asyncio.wait_for(started.wait(),2)
        task=service.tasks[job['id']]
        with pytest.raises(ValueError,match='正在运行'):service.start_cross_source('demo','peer','source')
        result=await service.cancel('source',job['id'],kind='cross_source')
        assert result['status']=='cancelled' and result['completed_at']
        assert task.done() and stopped.is_set() and not service.tasks
        assert not publications
        assert repo.get_package('source')['payload']['relations']==[{'name':'existing'}]
    asyncio.run(run())


def test_cancel_queued_task_before_first_execution_and_allow_new_analysis(monkeypatch):
    service,repo,_=setup_service()
    async def run():
        job=service.start('source',relations_only=True)
        assert (await service.cancel('source',job['id']))['status']=='cancelled'
        second=service.start('source',relations_only=True,fresh=True)
        assert second['id']!=job['id']
        assert second['details']['fresh'] is True
        await service.cancel('source',second['id'])
    asyncio.run(run())


def test_cancel_orphan_and_wrong_source_and_protect_publication():
    service,repo,_=setup_service()
    async def run():
        orphan=repo.create_job('source')
        with pytest.raises(KeyError):await service.cancel('other',orphan['id'])
        assert repo.get_job(orphan['id'])['status']=='queued'
        assert (await service.cancel('source',orphan['id']))['status']=='cancelled'
        publishing=repo.create_job('source')
        service._update(publishing,'building','publishing',90)
        with pytest.raises(ValueError,match='发布'):await service.cancel('source',publishing['id'])
        assert repo.get_job(publishing['id'])['status']=='building'
    asyncio.run(run())


def test_unrequested_shutdown_keeps_research_resumable(monkeypatch):
    service,repo,_=setup_service();started=asyncio.Event()
    async def review(*args,**kwargs):
        started.set();await asyncio.Event().wait()
    monkeypatch.setattr(service,'_semantic_review',review)
    async def run():
        job=service.start('source',relations_only=True)
        await started.wait()
        task=service.tasks[job['id']];task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        assert repo.get_job(job['id'])['status'] not in {'cancelled','completed','failed'}
    asyncio.run(run())


def test_relationship_only_analysis_uses_saved_schema_without_scan(monkeypatch):
    service,repo,sources=setup_service()
    async def review(candidates,table_maps,*args,**kwargs):
        assert set(table_maps)=={'source'}
        assert list(table_maps['source'])==['s.t']
    monkeypatch.setattr(service,'_semantic_review',review)
    async def run():
        job=service.start('source',relations_only=True,fresh=True)
        await service.tasks[job['id']]
        assert repo.get_job(job['id'])['status']=='completed',repo.get_job(job['id']).get('error')
        assert len(sources.get('source').scan_result['tables'])==1
    asyncio.run(run())


def test_api_cancel_and_reanalyze_contract(monkeypatch):
    service,repo,sources=setup_service()
    monkeypatch.setattr(database_routes,'datasource_service',DataSourceService(repository=sources))
    monkeypatch.setattr(database_routes,'modeling_service',service)
    job=repo.create_job('source')
    client=TestClient(main.app)
    assert client.post(f"/api/v1/datasources/source/onboarding/{job['id']}/cancel").json()['status']=='cancelled'
    assert client.post('/api/v1/datasources/source/onboarding/missing/cancel').status_code==404
    start=Mock(return_value={'id':'new','status':'queued'})
    monkeypatch.setattr(service,'start',start)
    assert client.post('/api/v1/datasources/source/relationships/analyze').status_code==202
    start.assert_called_once_with('source',relations_only=True,fresh=True)
    sources.get('source').scan_result['tables']=[]
    assert client.post('/api/v1/datasources/source/relationships/analyze').status_code==202
    sources.get('source').scan_result.pop('tables')
    assert client.post('/api/v1/datasources/source/relationships/analyze').status_code==409


def test_cancel_requested_job_is_not_resumed_after_restart():
    service,repo,_=setup_service()
    job=repo.create_job('source',details={'cancel_requested':True})
    service._update(job,'cancelling','cancelling',60)
    service.resume()
    assert repo.get_job(job['id'])['status']=='cancelled'
    assert service.tasks=={}


def test_reanalysis_bypasses_shared_cache_but_same_job_can_resume(monkeypatch,tmp_path):
    from semora_agent.model import structured_model
    service,repo,sources=setup_service()
    monkeypatch.setenv('RELATIONSHIP_CHECKPOINT_DIR',str(tmp_path))
    calls=[]
    async def model(system,user):
        calls.append(user)
        return {'keywords':[]}
    monkeypatch.setattr(structured_model,'complete_json',model)
    tables={t['id']:t for t in sources.get('source').scan_result['tables']}
    async def run():
        first=repo.create_job('source')
        await service._semantic_review([],{'source':tables},first)
        fresh=repo.create_job('source',details={'fresh':True})
        await service._semantic_review([],{'source':tables},fresh)
        assert len(calls)==2
        await service._semantic_review([],{'source':tables},fresh)
        assert len(calls)==2
    asyncio.run(run())
