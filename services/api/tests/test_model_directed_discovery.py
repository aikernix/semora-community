import asyncio
import json
import time

import pytest
from app.relationship_discovery import Discovery, DiscoveryIncomplete
from app.relationship_inputs import build_inputs, Checkpoints, digest
from app.datasource_modeling import ModelingRepository, package_objects, package_relations


def cards(n):
    tables = {f't{i}': {'id': f't{i}', 'name': f'table{i}', 'columns': [
        {'name': 'id', 'type': 'bigint', 'description': '记录编号'},
        {'name': 'body', 'type': 'clob', 'description': '大文本'},
        {'name': 'date', 'type': 'date', 'description': '日期'}],
        'primary_keys': [{'columns': ['id']}]} for i in range(n)}
    return build_inputs({'ds': tables})


class Model:
    def __init__(self):
        self.calls = []

    async def complete_json(self, system, encoded):
        payload = json.loads(encoded)
        self.calls.append(payload)
        assert len(payload.get('tables', [])) <= (20 if payload['phase'] == 'read' else 40)
        return {'relations': [], 'leads': [], 'requests': [], 'complete': True}


def test_thousand_tables_exactly_fifty_reads_and_zero_repeated_model_calls(tmp_path):
    model = Model()
    checkpoint = Checkpoints(tmp_path)
    state = checkpoint.load('run', 'fingerprint')
    source = cards(1000)
    def save(): checkpoint.save('run', state)
    asyncio.run(Discovery(source, model, state, save).run())
    assert len(model.calls) == 50
    assert len({c['table_id'] for call in model.calls for c in call['tables']}) == 1000
    restored = checkpoint.load('run', 'fingerprint')
    asyncio.run(Discovery(source, model, restored, lambda: None).run())
    assert len(model.calls) == 50
    assert restored['metrics']['cache_hits'] == 50


def test_raw_field_inputs_have_no_aliases_or_model_semantics():
    values = cards(2)
    for card in values.values():
        assert [c['name'] for c in card['columns']] == ['id']
        assert set(card['columns'][0]) == {'name', 'type', 'comment'}


def test_checkpoint_cleanup_only_removes_old_terminal_jobs(tmp_path):
    store = Checkpoints(tmp_path)
    store.save('old', {'terminal_at': time.time() - 8 * 86400})
    store.save('active', {})
    store.save('recent', {'terminal_at': time.time()})
    store.cleanup()
    assert not store.path('old').exists()
    assert store.path('active').exists() and store.path('recent').exists()
    with pytest.raises(ValueError): store.path('../secret')


def test_cross_batch_read_target_and_more_than_twelve_proposals():
    source = cards(40)
    ids = list(source)
    source[ids[0]]['columns'].extend({'name':f'ref{i}','type':'bigint','comment':f'reference role {i}'} for i in range(25))
    class Cross(Model):
        async def complete_json(self, system, encoded):
            p = json.loads(encoded); self.calls.append(p)
            response = {'relations': [], 'leads': [], 'complete': True}
            if p['phase'] == 'read' and ids[0] in [t['table_id'] for t in p['tables']]:
                response['leads'] = [{'source_table_id': ids[0], 'fields': ['ref0'], 'question': '目标编号', 'keywords': ['table']}]
            elif p['phase'] == 'compare' and sorted([ids[0], ids[30]]) in p['comparison_pairs']:
                response['relations'] = [{'source_table_id': ids[0], 'target_table_id': ids[30],
                    'key_pairs': [{'source': f'ref{i}', 'target': 'id'}], 'role': str(i)} for i in range(25)]
            elif p['phase']=='complete_references':
                response['leads'] = [{**l, 'keywords': []} for l in p['focus_leads']]
            return response
    model = Cross()
    result = asyncio.run(Discovery(source, model, {}, lambda: None).run())
    assert len(result) == 25


def test_missing_model_coverage_fails_instead_of_publishing_empty_result():
    class Broken(Model):
        async def complete_json(self, system, encoded): return {'relations': []}
    with pytest.raises(DiscoveryIncomplete):
        asyncio.run(Discovery(cards(1), Broken(), {}, lambda: None).run())


def test_repository_never_writes_candidates_and_package_only_persists_active_records():
    repo = ModelingRepository(dsn='')
    repo.replace_candidates([{'id': 'a', 'source_datasource_id': 'ds', 'scope': 'single_source', 'status': 'pending'}], 's', 's')
    assert repo.list_candidates('ds')[0]['status'] == 'pending'  # private workspace only
    package = repo.save_package('ds', 's', {'objects': [], 'relations': [], 'relationshipRecords': list(repo.memory_candidates.values())}, 'ready', 0)
    assert package['payload']['relationshipRecords'] == []


def test_model_failure_preserves_current_active_package(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.datasource_modeling import ModelingService
    from app.datasource_table_storage import tables_digest
    from semora_agent.model import structured_model
    tables = [{'id':'s.t','name':'t','columns':[{'name':'id','type':'bigint'}], 'primary_keys':[{'columns':['id']}]}]
    record = SimpleNamespace(id='ds', status='healthy', type='postgresql', secret_ref='DATASOURCE_DS_URL', scan_result={'tables':tables})
    repo = ModelingRepository(dsn='')
    original = repo.save_package('ds', tables_digest(tables), {'objects':[], 'relations':[{'name':'old'}]}, 'ready', 0)
    async def failure(*args, **kwargs): raise RuntimeError('provider failed')
    monkeypatch.setattr(structured_model, 'complete_json', failure)
    monkeypatch.setenv('RELATIONSHIP_CHECKPOINT_DIR', str(tmp_path))
    service = ModelingService(SimpleNamespace(repository=SimpleNamespace(get=lambda _:record)), failure, repository=repo)
    job = repo.create_job('ds')
    # Bypass capability/scanning, still exercise production failure/publication path.
    job['details']['persisted_only'] = True
    asyncio.run(service._run_onboarding(job))
    assert repo.get_package('ds') == original
    assert job['status'] == 'failed'


def test_shared_searches_and_comparisons_are_merged():
    source = cards(3); ids = list(source)
    class Duplicate(Model):
        async def complete_json(self, system, encoded):
            p=json.loads(encoded); self.calls.append(p)
            if p['phase']=='complete_references':
                return {'relations':[], 'leads':[{**l,'keywords':[]} for l in p['focus_leads']], 'complete':True}
            return {'relations': [], 'leads': [
                {'source_table_id': t['table_id'], 'fields': ['id'], 'question': '引用', 'keywords': ['table']}
                for t in p['tables']] if p['phase']=='read' else [], 'complete': True}
    m=Duplicate(); state={}
    asyncio.run(Discovery(source,m,state,lambda:None).run())
    assert len(m.calls)==3
    assert len(state['search_results'])==1


def test_missing_full_join_predicate_never_publishes():
    from app.relationship_evidence import finalize_relationships
    c={'status':'pending','evidence_grade':'medium','evidence':[
        {'kind':'explicit_field_reference'}, {'kind':'join_contract','valid':False},
        *[{'kind':kind,'supported':True,'confidence':.99,'target_role':'reference',
           'alternative_unresolved':False,'query_outcome':'usable','key_domain':'same'}
          for kind in ('model_semantic_review','independent_review')]]}
    finalize_relationships([c])
    assert c['status']!='accepted'
    assert 'join_predicates_unresolved' in c['evidence'][-1]['reasons']


@pytest.mark.parametrize('view, population, expected, duplicates', [(False,20,1,0),(True,20,1,0),(True,0,0,0),(True,20,1,3)])
def test_full_pipeline_publishes_cross_named_reference_and_persists_review_candidates(monkeypatch, tmp_path, view, population, expected, duplicates):
    from types import SimpleNamespace
    from semora_agent.model import structured_model
    from app.datasource_modeling import ModelingService
    tables=[{'id':'s.STUDENT','name':'STUDENT','description':'学生主表','columns':[
        {'name':'ID','type':'bigint','description':'学生编号'}],'primary_keys':[{'columns':['ID']}]},
        {'id':'s.ENROLLMENT','name':'ENROLLMENT','description':'选课记录','columns':[
        {'name':'STU_ID','type':'bigint','description':'所属学生编号'}], 'primary_keys':[]}]
    if view:
        tables[0]['kind']='VIEW'
        tables[0]['primary_keys']=[]
    record=SimpleNamespace(id='ds',status='healthy',type='postgresql',secret_ref='DATASOURCE_DS_URL',scan_result={'tables':tables})
    async def model(system, user):
        p=json.loads(user)
        if 'phase' in p:
            if p['phase']=='keywords':
                return {'keywords':['学生']}
            if p['phase']=='organization':
                return {'groups':[{'table_ids':g['table_ids'],
                                   'reason':'学生编号用途一致，保留分析'} for g in p['groups']], 'excluded':[]}
            ids={t['name']:t['table_id'] for t in p['tables']}
            if p['phase'] == 'read':
                return {'relations': [], 'leads': [{'source_table_id': ids['ENROLLMENT'], 'fields': ['STU_ID'], 'question': '学生引用', 'keywords': ['学生']}], 'complete': True}
            return {'relations':[{'source_table_id':ids['ENROLLMENT'],'target_table_id':ids['STUDENT'],
                'key_pairs':[{'source':'STU_ID','target':'ID'}],'reason':'选课记录的学生引用学生主表','role':'student','guards':[]}],
                'leads':[], 'requests':[], 'complete':True}
        return {'decisions':[{'candidate_id':c['candidate_id'],'supported':True,'structure_sufficient':True,'confidence':.98,
            'target_role':'reference','alternative_unresolved':False,'query_outcome':'usable','exclusion_code':'',
            'missing_evidence':[],'additional_predicates':[],'key_domain':'same','relationship_kind':'entity_reference',
            'coverage_expectation':'complete','reason':'所属学生编号引用学生主键','query_role':'student'} for c in p['candidates']]}
    async def executor(method,path,**kwargs):
        if path=='datasources/relation-definitions':
            return {'definition':'SELECT ID FROM storage'}
        if path=='query':
            assert kwargs['json']['timeout_ms']==10000
            sql=kwargs['json']['sql']
            assert 'LIMIT 1' in sql
            return {'rows':[[duplicates if 'duplicates' in sql else population]]}
        assert path=='datasources/profile-relationship'
        return {'source_nonnull_distinct_keys':10,'source_exhausted':True,
                'directional_coverage':1. if population else 0.,'matched_keys':10 if population else 0}
    monkeypatch.setattr(structured_model,'complete_json',model)
    monkeypatch.setenv('RELATIONSHIP_CHECKPOINT_DIR',str(tmp_path))
    repo=ModelingRepository(dsn='')
    service=ModelingService(SimpleNamespace(repository=SimpleNamespace(get=lambda _:record)),executor,repository=repo)
    job=repo.create_job('ds')
    asyncio.run(service._run_onboarding(job))
    assert job['status']=='completed',job.get('error')
    package=repo.get_package('ds')
    assert package['relation_count']==expected
    if expected:
        assert package['payload']['relationshipRecords'][0]['key_pairs']==[{'source':'STU_ID','target':'ID'}]
        if duplicates:
            assert package['payload']['relationshipRecords'][0]['cardinality']=='N:N'
    assert len(repo.memory_candidates)==1
    persisted=next(iter(repo.memory_candidates.values()))
    assert persisted['source_datasource_id']=='ds'
    assert persisted['status']==('accepted' if expected else 'unresolved')
    assert 'query_table_roles' not in job['details']


def test_research_pending_completes_other_results_without_publishing_pending_candidate(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.datasource_modeling import ModelingService
    tables = [
        {'id':'s.child','name':'child','columns':[{'name':'parent_id','type':'bigint'}], 'primary_keys':[],
         'foreign_keys':[{'columns':['parent_id'],'target_table':'s.parent','target_columns':['id']}]},
        {'id':'s.parent','name':'parent','columns':[{'name':'id','type':'bigint'}], 'primary_keys':[{'columns':['id']}]},
    ]
    record = SimpleNamespace(id='ds',status='healthy',type='postgresql',secret_ref='X',scan_result={'tables':tables})
    repo = ModelingRepository(dsn='')
    async def executor(*args,**kwargs): raise AssertionError('unexpected executor call')
    service = ModelingService(SimpleNamespace(repository=SimpleNamespace(get=lambda _:record)),executor,repository=repo)
    async def review(candidates, table_maps, job=None, persisted_only=False, discover=True):
        candidates[0]['evidence_grade']='medium'
        candidates[0]['status']='pending'
        candidates[0]['evidence'].append({'kind':'research_gap','reason':'ValidationError'})
        job['details']['research_pending']=1
    monkeypatch.setattr(service,'_semantic_review',review)
    monkeypatch.setenv('RELATIONSHIP_CHECKPOINT_DIR',str(tmp_path))
    job = repo.create_job('ds',details={'persisted_only':True})
    asyncio.run(service._run_onboarding(job))
    assert job['status']=='completed_with_pending'
    assert job['details']['research_pending']==1
    assert repo.get_package('ds')['relation_count']==0


def test_failed_publication_preserves_old_active_package(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.datasource_modeling import ModelingService
    from app.datasource_table_storage import tables_digest
    tables=[{'id':'s.t','name':'t','columns':[{'name':'id','type':'bigint'}],'primary_keys':[]}]
    record=SimpleNamespace(id='ds',status='healthy',type='postgresql',secret_ref='DATASOURCE_DS_URL',scan_result={'tables':tables})
    repo=ModelingRepository(dsn='')
    old=repo.save_package('ds',tables_digest(tables),{'objects':[],'relations':[{'name':'old'}]},'ready',0)
    service=ModelingService(SimpleNamespace(repository=SimpleNamespace(get=lambda _:record)),None,repository=repo,ontology_service=object())
    async def review(*args,**kwargs):pass
    async def publish(*args,**kwargs):raise RuntimeError('publication failed')
    monkeypatch.setattr(service,'_semantic_review',review)
    monkeypatch.setattr(service,'_commit_source_package',publish)
    monkeypatch.setenv('RELATIONSHIP_CHECKPOINT_DIR',str(tmp_path))
    job=repo.create_job('ds')
    asyncio.run(service._run_onboarding(job))
    assert job['status']=='failed'
    assert repo.get_package('ds')==old


def test_overlapping_reworded_leads_are_not_recursive_new_tasks():
    source=cards(3); ids=list(source)
    class Reworded(Model):
        async def complete_json(self, system, encoded):
            p=json.loads(encoded); self.calls.append(p)
            if p['phase']=='complete_references':
                return {'relations': [], 'leads': [dict(lead, keywords=[]) for lead in p['focus_leads']], 'complete': True}
            return {'relations': [], 'leads': [{'source_table_id':ids[0],'fields':['id'],
                'question':'different wording '+p['phase'], 'keywords':['table']}], 'complete': True}
    model=Reworded()
    asyncio.run(Discovery(source,model,{},lambda:None).run())
    assert [call['phase'] for call in model.calls]==['read','compare','complete_references']


def test_dialect_selection_is_explicit_for_compatible_products():
    from types import SimpleNamespace
    from app.relationship_inputs import dialect_settings
    assert dialect_settings(SimpleNamespace(type='oracle',scan_result={}))==('oracle','rownum')
    assert dialect_settings(SimpleNamespace(type='sqlserver',scan_result={}))[0]=='tsql'
    assert dialect_settings(SimpleNamespace(type='oceanbase',scan_result={'connection':{'compatibility_mode':'mysql'}}))[0]=='mysql'
    with pytest.raises(ValueError):
        dialect_settings(SimpleNamespace(type='jdbc',scan_result={}))
