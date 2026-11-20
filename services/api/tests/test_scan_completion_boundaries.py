import asyncio
from copy import deepcopy
from types import SimpleNamespace
import pytest
from app.datasources import DataSourceRecord, DataSourceRepository
from app.datasource_modeling import ModelingRepository, ModelingService


def table(name,columns=('id',)):
    return {'id':'s.'+name,'schema':'s','name':name,'columns':[{'name':c,'type':'bigint'} for c in columns],
            'primary_keys':[],'foreign_keys':[],'unique_constraints':[]}


def complete(tables):
    return {'status':'completed','tables':tables,'permission_gaps':[],
            'connection':{'version':'1','read_only':True},
            'scan_manifest':{'complete':True,'scope':'full','scanned_tables':len(tables)}}


def repository():
    r=DataSourceRepository(dsn='');item=DataSourceRecord('scan','Source','postgresql','UNUSED',status='healthy')
    item.scan_result={'tables':[table('keep'),table('remove')],'connection':{'version':'1'}}
    r.save(item);return r


@pytest.mark.parametrize('patch',[{'status':'failed'}, {'status':'cancelled'}, {'partial':True},
    {'permission_gaps':[{'area':'columns','status':'unavailable'}]},
    {'scan_manifest':{'complete':False,'scope':'full','scanned_tables':1}},
    {'scan_manifest':{'complete':True,'scope':'selected','scanned_tables':1}},
    {'scan_manifest':{'complete':True,'scope':'full','scanned_tables':20}},
    {'scan_manifest':{}}, {'tables':[{'id':'s.keep','columns':[]}]}])
def test_incomplete_scan_does_not_mutate_previous_schema(patch):
    r=repository();before=deepcopy(r.get('scan'))
    value={**complete([table('keep')]),**patch}
    with pytest.raises(ValueError):r.record_scan('scan',value)
    assert r.get('scan')==before


def test_full_scan_reconciles_schema_but_does_not_analyze_or_replace_relationships():
    r=repository();packages=ModelingRepository(dsn='')
    old=packages.save_package('scan','old',{'objects':[],'relations':[{'name':'keep_active'}]},'ready',0)
    updated=r.record_scan('scan',complete([table('keep',('id','new_ref')),table('added')]))
    assert [t['id'] for t in updated.scan_result['tables']]==['s.keep','s.added']
    assert updated.scan_result['scan_diff']=={'added':['s.added'],'removed':['s.remove']}
    assert 'relationship_candidates' not in updated.scan_result
    assert packages.get_package('scan')==old


def test_metadata_onboarding_stops_before_modeling(monkeypatch):
    r=repository();r.get('scan').scan_result={}
    calls=[]
    async def executor(method,path,**kwargs):
        calls.append(path);assert path=='datasources/scan'
        return complete([table('keep')])
    async def forbidden(*args,**kwargs):raise AssertionError('model must not run during metadata onboarding')
    source=SimpleNamespace(repository=r,test=lambda *a:None)
    service=ModelingService(source,executor,repository=ModelingRepository(dsn=''))
    monkeypatch.setattr(service,'_semantic_review',forbidden)
    async def validate(*args,**kwargs):return None
    monkeypatch.setattr('app.capability_service.validate_cached',validate)
    job=service.repository.create_job('scan',details={'metadata_only':True})
    asyncio.run(service._run_onboarding(job))
    assert job['status']=='completed',job.get('error')
    assert job['phase']=='metadata_complete' and job['details']['relationships_updated'] is False
    assert calls==['datasources/scan'] and not service.repository.memory_packages


def test_create_source_schedules_metadata_only(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    r=DataSourceRepository(dsn='');calls=[]
    def start(identifier,**options):
        calls.append(options);return {'id':'metadata-job'}
    monkeypatch.setattr(main,'datasource_service',SimpleNamespace(repository=r))
    monkeypatch.setattr(main,'modeling_service',SimpleNamespace(start=start))
    response=TestClient(main.app).post('/api/v1/datasources',json={
        'name':'Source','type':'postgresql','secret_ref':'DATASOURCE_TEST_URL'})
    assert response.status_code==201,response.text
    assert calls==[{'metadata_only':True}]
