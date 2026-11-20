"""Recall organization is isolated from relation approval and external services."""
import asyncio
import json

import pytest

from app.relationship_inputs import build_inputs
from app.relationship_keywords import KeywordDiscovery
from app.relationship_contracts import OrganizationOutput
from app.relationship_organization import ORGANIZATION_GROUP_LIMIT, validate_organization


def source(n=4):
    return build_inputs({'source':{str(i):{'id':str(i),'name':f'entry_{i}',
        'comment':'业务对象记录','columns':[
            {'name':'id','type':'bigint','comment':'记录主键'},
            {'name':'object_ref','type':'bigint','comment':'对象编号'},
            {'name':'unmatched_code','type':'varchar(50)','comment':'其他用途'}]}
        for i in range(n)}})


def prepared(cards,model,state=None):
    engine=KeywordDiscovery(cards,model,state if state is not None else {},lambda:None)
    engine.work.claim('keywords','batch')
    engine.work.finish('keywords','batch',{'keywords':['object_ref']},['object_ref'])
    engine.finish_stage('keywords',{'keyword_count':1})
    asyncio.run(engine.retrieve_all())
    return engine


def retained(payload):
    return {'groups':[{'table_ids':g['table_ids'],
                       'reason':'整组适合一起分析'} for g in payload['groups']]}


def test_organization_timeout_falls_back_to_complete_original_group(monkeypatch):
    monkeypatch.setattr('app.relationship_organization.ORGANIZATION_GROUP_LIMIT',1)
    class Model:
        async def complete_json(self,system,user):
            if json.loads(user)['phase']=='organization':raise TimeoutError('temporary timeout')
            return {'relations':[]}
    engine=prepared(source(3),Model())
    organized=asyncio.run(engine.organize())
    assert set(organized)=={tuple(sorted(engine.cards))}
    assert not engine.incomplete_units()
    assert asyncio.run(engine.discover())==[]
    assert engine.scope['discovery_complete'] is True


def test_split_large_group_drives_actual_comparisons_and_resumes_without_model_calls(monkeypatch):
    monkeypatch.setattr('app.relationship_organization.ORGANIZATION_GROUP_LIMIT',2)
    class Model:
        calls=[]
        async def complete_json(self,system,user):
            p=json.loads(user);self.calls.append(p)
            if p['phase']=='organization':
                g=p['groups'][0]
                assert len(p['groups'])==1
                assert 'group_id' not in g
                assert all([f['name'] for f in t['matched_fields']]==['object_ref'] for t in p['tables'])
                assert all('columns' not in t for t in p['tables'])
                return {'groups':[{'table_ids':ids,
                    'reason':'字段注释明确为不同编号体系'} for ids in [g['table_ids'][:2],g['table_ids'][2:]]], 'excluded':[]}
            assert p['phase']=='compare'
            return {'relations':[]}
    model=Model();state={};engine=prepared(source(4),model,state)
    asyncio.run(engine.organize())
    assert asyncio.run(engine.discover())==[]
    compared=[pair for p in model.calls if p['phase']=='compare' for pair in p['comparison_pairs']]
    assert len([pair for pair in compared if pair[0]!=pair[1]])==2
    assert engine.stage_result('organization')['metrics']['output_groups']==2
    before=len(model.calls)
    restored=KeywordDiscovery(engine.cards,model,state,lambda:None)
    assert asyncio.run(restored.discover())==[]
    assert len(model.calls)==before


def test_omitted_tables_are_skipped_without_exclusion_output(monkeypatch):
    monkeypatch.setattr('app.relationship_organization.ORGANIZATION_GROUP_LIMIT',2)
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1;p=json.loads(user);g=p['groups'][0]
            return {'groups':[{'table_ids':g['table_ids'][:2],
                               'reason':'两表对象编号含义相同'}]}
    engine=prepared(source(3),Model())
    output=asyncio.run(engine.organize())
    assert [len(ids) for ids in output]==[2]
    saved=engine.stage_result('organization')
    omitted_table=sorted(set(engine.cards)-set(next(iter(output))))[0]
    assert saved['omitted']==[{'table_ids':[omitted_table]}]
    assert saved['metrics']['omitted_memberships']==1
    assert engine.work.keywords()==['object_ref']
    assert 'discovery_complete' not in engine.scope
    assert asyncio.run(engine.organize())==output
    assert engine.model.calls==1


def test_pending_groups_are_sent_one_at_a_time_and_reuse_cached_organization(monkeypatch):
    monkeypatch.setattr('app.relationship_organization.ORGANIZATION_GROUP_LIMIT',1)
    class Model:
        calls=0; requests=[]
        async def complete_json(self,system,user):
            self.calls+=1;p=json.loads(user);self.requests.append(p)
            return retained(p)
    cards=source(3);ids=tuple(sorted(cards));model=Model()
    engine=prepared(cards,model)
    groups={ids[:2]:['object_ref'],ids[1:]:['对象']}
    result=asyncio.run(engine.organize_groups(groups))
    assert [len(p['groups']) for p in model.requests]==[1,1]
    assert result==groups
    assert asyncio.run(engine.organize_groups(groups))==result and model.calls==2


def test_invalid_model_organization_is_discarded_after_retries_without_failing_stage(monkeypatch):
    monkeypatch.setattr('app.relationship_organization.ORGANIZATION_GROUP_LIMIT',2)
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1;p=json.loads(user);g=p['groups'][0]
            return {'groups':[{'table_ids':g['table_ids']+['missing-table'],
                               'reason':'包含编造表'}]}
    model=Model();engine=prepared(source(3),model)
    assert list(asyncio.run(engine.organize()))==[tuple(sorted(engine.cards))]
    assert model.calls==3
    assert engine.stage_result('organization')['mode']=='selected_groups'
    assert len(engine.scope['discarded_groups'])==1


def test_organization_salvage_keeps_valid_sibling_group():
    from app.relationship_organization import salvage_organization
    payload={'groups':[{'table_ids':['a','b','c']}]}
    result,discarded=salvage_organization({'groups':[
        {'table_ids':['a','b'],'reason':'合法组'},
        {'table_ids':['c','missing'],'reason':'包含编造表'}]},payload)
    assert result=={'groups':[{'table_ids':['a','b'],'reason':'合法组'}]}
    assert len(discarded)==1 and discarded[0]['error']=='unknown_organized_table'


def test_empty_selection_skips_the_original_group(monkeypatch):
    monkeypatch.setattr('app.relationship_organization.ORGANIZATION_GROUP_LIMIT',2)
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1
            return {'groups':[]}
    cards=source(3);model=Model();engine=prepared(cards,model)
    result=asyncio.run(engine.organize())
    assert result=={}
    assert engine.stage_result('organization')['mode']=='selected_groups'
    assert engine.stage_result('organization')['metrics']['omitted_memberships']==3
    assert model.calls==1


def test_oversized_summary_retains_whole_group_without_losing_cross_page_pairs(monkeypatch):
    monkeypatch.setattr('app.relationship_organization.ORGANIZATION_GROUP_LIMIT',2)
    class Forbidden:
        async def complete_json(self,*args):raise AssertionError('oversized request')
    engine=prepared(source(4),Forbidden())
    monkeypatch.setattr('app.relationship_organization.REQUEST_LIMIT',100)
    result=asyncio.run(engine.organize())
    assert list(result)==[tuple(sorted(engine.cards))]
    assert engine.scope['organization_metrics']['deferred_groups']==1
    assert engine.scope['organization_metrics']['omitted_memberships']==0


def test_output_contract_allows_partial_selection_but_rejects_empty_groups():
    with pytest.raises(ValueError):
        OrganizationOutput.model_validate({'groups':[{'table_ids':[], 'reason':'x'}]})
    singleton=OrganizationOutput.model_validate({'groups':[{'table_ids':['t'], 'reason':'x'}]})
    assert singleton.groups[0].table_ids==['t']
    assert 'group_id' not in OrganizationOutput.model_json_schema()['$defs']['OrganizedGroup']['properties']
    payload={'groups':[{'table_ids':['a','b','c']}]}
    validate_organization({'groups':[{'table_ids':['a','b'],'reason':'只分析这两张表'}]},payload)
    validate_organization({'groups':[]},payload)
    with pytest.raises(ValueError,match='unknown_organized_table'):
        validate_organization({'groups':[{'table_ids':['missing'],'reason':'编造表'}]},payload)


def test_multiple_output_groups_share_the_internal_original_group_id():
    from app.relationship_organization import effective_group
    original={'group_id':'internal1','table_ids':['a','b','c','d'],'keywords':['object']}
    view=effective_group({'groups':[{'table_ids':['a','b'],'reason':'第一组'},
                                  {'table_ids':['c','d'],'reason':'第二组'}]},original)
    assert [g['group_id'] for g in view['groups']]==['internal1','internal1']
    assert view['omitted_table_ids']==[]


def test_singleton_split_is_valid_but_does_not_create_comparison_group(monkeypatch):
    monkeypatch.setattr('app.relationship_organization.ORGANIZATION_GROUP_LIMIT',2)
    class Model:
        calls=[]
        async def complete_json(self,system,user):
            p=json.loads(user);self.calls.append(p)
            if p['phase']=='compare':return {'relations':[]}
            g=p['groups'][0]
            return {'groups':[
                {'table_ids':g['table_ids'][:1],'reason':'独立用途'},
                {'table_ids':g['table_ids'][1:],'reason':'相同编号体系'}], 'excluded':[]}
    cards=source(3);model=Model();engine=prepared(cards,model)
    result=asyncio.run(engine.organize())
    assert [len(ids) for ids in result]==[2]
    assert engine.scope['organization_metrics']['single_table_groups']==1
    assert asyncio.run(engine.discover())==[]
    compared=[pair for p in model.calls if p['phase']=='compare' for pair in p['comparison_pairs']]
    assert all(min(cards) not in pair for pair in compared)


def test_small_groups_skip_organization_model_without_losing_tables():
    class Forbidden:
        async def complete_json(self,*args):raise AssertionError('small group must not call organization')
    cards=source(2);engine=prepared(cards,Forbidden())
    result=asyncio.run(engine.organize())
    assert list(result)==[tuple(sorted(cards))]
    assert engine.scope['organization_metrics']['skipped_small_groups']==1
    assert engine.scope['organization_metrics']['deferred_groups']==0


def test_fifty_four_table_summary_is_read_in_one_bounded_request():
    from app.relationship_context import REQUEST_LIMIT, request_size
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1;p=json.loads(user)
            assert len(p['groups'])==1
            assert len(p['tables'])==54
            assert request_size(system,p)<=REQUEST_LIMIT
            assert all('matched_fields' in t and 'columns' not in t for t in p['tables'])
            return retained(p)
    model=Model();engine=prepared(source(54),model)
    result=asyncio.run(engine.organize())
    assert len(result)==1 and len(next(iter(result)))==54 and model.calls==1


def test_organization_limit_is_twenty_tables():
    assert ORGANIZATION_GROUP_LIMIT==20
