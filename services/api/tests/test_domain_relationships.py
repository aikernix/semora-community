from copy import deepcopy
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import database_routes
from app.datasource_modeling import ModelingRepository,ModelingService,_candidate,tables_digest


@pytest.fixture
def studio(monkeypatch):
    a={'id':'public.events','name':'events','columns':[{'name':'owner_ref','type':'bigint'}]}
    b={'id':'crm.entities','name':'entities','columns':[{'name':'id','type':'bigint'}]}
    sources={'a':SimpleNamespace(id='a',scan_result={'tables':[a]}),'b':SimpleNamespace(id='b',scan_result={'tables':[b]})}
    repository=ModelingRepository(dsn='')
    service=ModelingService(SimpleNamespace(repository=SimpleNamespace(get=lambda sid:sources[sid])),None,repository=repository)
    configuration={'domain':'fixture','name':'Test','sourcePackageRefs':[{'dataSourceId':'a'},{'dataSourceId':'b'}],
        'objects':[{'name':'event','table':a['id'],'dataSourceId':'a','properties':[{'name':'owner_ref','column':'owner_ref'}]},
                   {'name':'entity','table':b['id'],'dataSourceId':'b','properties':[{'name':'id','column':'id'}]}],
        'relations':[{'name':'active','from':'event','to':'entity','condition':'event.owner_ref = entity.id','cardinality':'N:1'},
                     {'name':'internal','from':'event','to':'event','condition':'event.owner_ref = event.owner_ref','cardinality':'1:1'}]}
    class Ontology:
        async def get_configuration(self,domain):
            assert domain=='fixture';return deepcopy(configuration)
        async def save_configuration(self,domain,value):
            configuration.clear();configuration.update(deepcopy(value));return deepcopy(value)
        async def unbind_source(self,domain,sid):return {'domain':domain,'objects':[],'relations':[],'sourcePackageRefs':[]}
    for identifier,status,card in [('pending','pending','N:1'),('uncertain','pending','unknown'),('disabled','disabled','N:1')]:
        record=_candidate('a','b',a,['owner_ref'],b,['id'],'cross_source',[],status=status,domain='fixture')
        record.update(id=identifier,cardinality=card)
        repository.replace_candidates([record],tables_digest([a]),tables_digest([b]))
    monkeypatch.setattr(database_routes,'modeling_service',service)
    monkeypatch.setattr(database_routes,'ontology_service',Ontology())
    app=FastAPI();app.include_router(database_routes.router)
    return TestClient(app),configuration,repository


def test_cross_review_list_excludes_internal_and_retains_all_states(studio):
    client,_,_=studio
    result=client.get('/api/v1/ontology/configurations/fixture/relationships')
    assert result.status_code==200,result.text
    assert result.json()['counts']=={'accepted':1,'pending':2,'disabled':1}
    assert all(r['relation']['name']!='internal' for r in result.json()['items'])


def test_cross_edit_activates_pending_and_preserves_internal_relations(studio):
    client,configuration,repository=studio
    relation={'name':'fixed','from':'event','to':'entity','cardinality':'N:1','condition':'event.owner_ref = entity.id'}
    response=client.put('/api/v1/ontology/configurations/fixture/relationships/pending',json=relation)
    assert response.status_code==200,response.text
    assert repository.memory_candidates['pending']['status']=='accepted'
    assert repository.memory_candidates['pending']['key_pairs']==[{'source':'owner_ref','target':'id'}]
    assert any(r['name']=='internal' for r in configuration['relations'])


def test_unknown_cross_cannot_be_approved_but_can_be_rejected(studio):
    client,_,repository=studio
    root='/api/v1/ontology/configurations/fixture/relationships/uncertain/decision'
    assert client.post(root,json={'action':'approve'}).status_code==409
    assert client.post(root,json={'action':'reject'}).status_code==200
    assert repository.memory_candidates['uncertain']['status']=='disabled'
    assert client.post(root,json={'action':'enable'}).status_code==409


def test_cross_edit_rejects_internal_mapping_and_wrong_domain(studio):
    client,_,_=studio
    body={'name':'bad','from':'event','to':'event','condition':'event.owner_ref = event.owner_ref','cardinality':'1:1'}
    assert client.put('/api/v1/ontology/configurations/fixture/relationships/pending',json=body).status_code==422
    assert client.put('/api/v1/ontology/configurations/fixture/relationships/missing',json=body).status_code==404


def test_manual_condition_survives_disable_and_enable(studio):
    client,configuration,repository=studio
    root='/api/v1/ontology/configurations/fixture/relationships/'
    relation={'name':'active','from':'event','to':'entity','cardinality':'N:1',
              'condition':'event.owner_ref = entity.id AND event.owner_ref IS NOT NULL'}
    assert client.put(root+'active',json=relation).status_code==200
    identifier=configuration['relations'][-1]['metadata']['candidateId']
    assert identifier!='active'
    assert client.post(root+identifier+'/decision',json={'action':'disable'}).status_code==200
    assert all(r['name']!='active' for r in configuration['relations'])
    assert client.post(root+identifier+'/decision',json={'action':'enable'}).status_code==200
    assert configuration['relations'][-1]['condition']==relation['condition']


def test_unbind_only_clears_candidates_in_current_domain(studio):
    client,_,repository=studio
    record=deepcopy(repository.memory_candidates['pending'])
    record.update(id='other-domain',domain='elsewhere')
    repository.replace_candidates([record],'a','b')
    result=client.delete('/api/v1/ontology/configurations/fixture/sources/a')
    assert result.status_code==200,result.text
    assert set(repository.memory_candidates)=={'other-domain'}


def test_cross_pair_history_is_unordered_scoped_and_survives_zero_results(studio):
    client,_,repository=studio
    first=repository.create_job('a',kind='cross_source',peer_datasource_id='b',domain='fixture')
    first['status']='completed';repository.save_job(first)
    latest=repository.create_job('b',kind='cross_source',peer_datasource_id='a',domain='fixture')
    latest.update(status='completed',details={'accepted':0});repository.save_job(latest)
    repository.create_job('a',kind='cross_source',peer_datasource_id='b',domain='other')
    repository.create_job('a',kind='cross_source',peer_datasource_id='unbound',domain='fixture')
    result=client.get('/api/v1/ontology/configurations/fixture/cross-source-analysis')
    assert result.status_code==200,result.text
    jobs=result.json()['items']
    assert len(jobs)==1 and jobs[0]['id']==latest['id']
    assert jobs[0]['details']['accepted']==0


def test_existing_unordered_pair_can_only_be_reanalyzed(studio, monkeypatch):
    client, configuration, repository = studio
    endpoint = '/api/v1/ontology/configurations/fixture/cross-source-analysis'
    body = {'left_source_id': 'b', 'right_source_id': 'a'}
    started = []
    monkeypatch.setattr(database_routes.modeling_service, 'start_cross_source',
                        lambda *args: started.append(args) or {'id': 'analysis'})
    assert client.post(endpoint, json=body).status_code == 409
    assert started == []
    assert client.post(endpoint, json={**body, 'mode': 'reanalyze'}).status_code == 202
    assert started == [('fixture', 'b', 'a')]

    configuration['relations'] = []
    repository.memory_candidates.clear()
    job = repository.create_job('a', kind='cross_source', peer_datasource_id='b', domain='fixture')
    repository.save_job({**job, 'status': 'completed'})
    assert client.post(endpoint, json=body).status_code == 409
    assert client.delete('/api/v1/ontology/configurations/fixture/sources/a').status_code == 200
    assert repository.domain_cross_jobs('fixture') == []
    assert client.post(endpoint, json=body).status_code == 202


def test_cross_cancel_is_domain_scoped_idempotent_and_protects_publication(studio):
    client,_,repository=studio
    job=repository.create_job('a',kind='cross_source',peer_datasource_id='b',domain='fixture')
    root=f'/api/v1/ontology/configurations/fixture/cross-source-analysis/{job["id"]}/cancel'
    assert client.post(root.replace('/fixture/','/elsewhere/')).status_code==404
    assert client.post(root).json()['status']=='cancelled'
    assert client.post(root).json()['status']=='cancelled'
    publishing=repository.create_job('a',kind='cross_source',peer_datasource_id='b',domain='fixture')
    publishing.update(status='building',phase='publishing');repository.save_job(publishing)
    assert client.post(root.replace(job['id'],publishing['id'])).status_code==409
