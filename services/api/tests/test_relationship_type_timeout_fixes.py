import asyncio
import pytest
from app.relationship_types import relationship_family
from app.relationship_context import complete
from semora_agent.model_diagnostics import current_delivery_budget


@pytest.mark.parametrize('a,b', [('NUMBER','NUMBER'),('NUMBER','NUMBER(20,0)'),('numeric','bigint'),('decimal(38,2)','NUMBER'),('varchar2(50)','nvarchar(20)')])
def test_compatible_families_do_not_need_export_precision(a,b):
    assert relationship_family({'type':a})==relationship_family({'type':b})


def test_unknown_is_not_mislabeled_as_a_known_compatible_type():
    assert relationship_family({'type':'UNRECOGNIZED_CUSTOM_TYPE'}) is None
    assert relationship_family({'type':'NUMBER'})!=relationship_family({'type':'varchar2(50)'})


def test_delivery_budget_is_scoped_to_relationship_call(monkeypatch):
    monkeypatch.setenv('RELATIONSHIP_NO_DELIVERY_SECONDS','300')
    class Model:
        async def complete_json(self,*args):
            value=current_delivery_budget()
            assert value[0]==300
            assert '关联分析' in value[1]
            await asyncio.sleep(0)
            return {}
    async def run():
        assert current_delivery_budget() is None
        await complete(Model(),'generic',{'tables':[]})
        assert current_delivery_budget() is None
    asyncio.run(run())


def test_numeric_reference_keeps_numeric_ontology_type():
    from app.datasource_modeling import _ontology_type
    assert _ontology_type({'type':'NUMBER'})=='decimal'


@pytest.mark.parametrize("identifier_type", ["varchar(50)", "VENDOR_IDENTIFIER", "NUMBER"])
def test_model_proposals_are_not_filtered_by_type_family(monkeypatch,tmp_path,identifier_type):
    import json
    from types import SimpleNamespace
    from app.datasource_modeling import ModelingRepository,ModelingService
    from semora_agent.model import structured_model
    tables=[{'id':'s.parent','name':'parent','description':'实体资料','columns':[{'name':'id','type':'NUMBER','description':'实体编号'}],'primary_keys':[{'columns':['id']}]},
            {'id':'s.child','name':'child','description':'引用实体的记录','columns':[{'name':'parent_id','type':'NUMBER','description':'所属实体编号'},{'name':'note','type':identifier_type,'description':'另一用途的实体编号'}]}]
    record=SimpleNamespace(id='ds',status='healthy',type='postgresql',scan_result={'tables':tables})
    async def model(system,user):
        p=json.loads(user)
        if p.get('phase')=='keywords':
            return {'keywords':['实体']}
        if p.get('phase')=='organization':
            return {'groups':[{'table_ids':g['table_ids'],
                               'reason':'实体编号线索一致，保留待分析'} for g in p['groups']], 'excluded':[]}
        if p.get('phase')=='complete_references':
            return {'relations':[],'leads':[{**l,'keywords':[]} for l in p['focus_leads']],'complete':True}
        if p.get('phase')=='read':
            ids={t['name']:t['table_id'] for t in p['tables']}
            return {'relations':[],'leads':[{'source_table_id':ids['child'],'fields':['parent_id'],'question':'引用目标','keywords':['实体']}],'complete':True}
        if p.get('phase')=='compare':
            ids={t['name']:t['table_id'] for t in p['tables']}
            return {'relations':[{'source_table_id':ids['child'],'target_table_id':ids['parent'],'key_pairs':[{'source':f,'target':'id'}],'role':'reference','reason':'引用','guards':[]} for f in ['parent_id','note']],'leads':[],'complete':True}
        return {'decisions':[{'candidate_id':c['candidate_id'],'supported':True,'structure_sufficient':True,'target_role':'reference','alternative_unresolved':False,'reason':'记录的所属实体编号引用实体唯一键','query_outcome':'usable','key_domain':'same','relationship_kind':'entity_reference','coverage_expectation':'complete','additional_predicates':[]} for c in p['candidates']]}
    monkeypatch.setattr(structured_model,'complete_json',model)
    monkeypatch.setenv('RELATIONSHIP_CHECKPOINT_DIR',str(tmp_path))
    repository=ModelingRepository(dsn='')
    service=ModelingService(SimpleNamespace(repository=SimpleNamespace(get=lambda _:record)),None,repository=repository)
    async def profile(*args):
        return {'source_nonnull_distinct_keys': 2, 'source_exhausted': True, 'directional_coverage': 1}
    monkeypatch.setattr(service, '_profile', profile)
    async def run():
        job=service.start('ds',relations_only=True,fresh=True)
        await service.tasks[job['id']]
        assert job['status']=='completed',job.get('error')
        relations=repository.get_package('ds')['payload']['relationshipRecords']
        assert len(relations)==2
        assert {r['key_pairs'][0]['source'] for r in relations}=={'parent_id','note'}
        data=json.loads((tmp_path/(job['id']+'.json')).read_text())
        assert not data.get('type_decisions')
    asyncio.run(run())
