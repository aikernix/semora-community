import asyncio
import itertools
import json
from collections import Counter

from app.relationship_discovery import Discovery, SYSTEM
from app.relationship_context import request_size, complete, REQUEST_LIMIT
from app.relationship_review_policy import review_reasons
from test_model_directed_discovery import cards


def test_search_ignores_json_properties_ids_types_and_schema_prefix():
    values=cards(4)
    for i,c in enumerate(values.values()):
        c['physical_table']='schema_agent.'+c['name']
        c['columns'][0]['type']='timestamp'
        if i==0:c['columns'][0]['name']='agent_id'
    d=Discovery(values,None,{},lambda:None)
    assert d.catalog('agent')['total']==1
    assert d.catalog('im')['total']==0
    assert d.catalog('timestamp')['total']==0
    assert d.catalog('PRIMARY_KEYS')['total']==0
    found=d.catalog('AGENT')['items'][0]
    assert found['columns'][0]['name']=='agent_id'
    assert 'columns.agent_id.name' in found['matches']


def test_hundred_tables_have_fifteen_tasks_with_exact_pair_coverage():
    values=cards(100); ids=tuple(sorted(values))
    d=Discovery(values,None,{},lambda:None)
    tasks=d.comparison_tasks({ids:[]},set())
    assert len(tasks)==15
    assert sum(len(t['tables'])<=20 for t in tasks)==5
    counts=Counter(tuple(p) for t in tasks for p in t['comparison_pairs'])
    assert set(counts)==set(itertools.combinations_with_replacement(ids,2))
    assert set(counts.values())=={1}
    assert max(len(t['tables']) for t in tasks)==40


def test_overlapping_groups_do_not_repeat_table_pairs():
    values=cards(45); ids=tuple(sorted(values));d=Discovery(values,None,{},lambda:None)
    tasks=d.comparison_tasks({ids[:30]:[],ids[15:]:[]},set())
    seen=[tuple(p) for t in tasks for p in t['comparison_pairs']]
    expected=set(itertools.combinations_with_replacement(ids[:30],2)) | set(itertools.combinations_with_replacement(ids[15:],2))
    assert set(seen)==expected
    assert len(seen)==len(expected)


def test_oversized_regions_preserve_cross_pairs():
    values=cards(40)
    for c in values.values():c['comment']='结构说明'*500
    d=Discovery(values,None,{},lambda:None);ids=tuple(sorted(values))
    expected=set(itertools.combinations(ids,2))
    tasks=list(d.regions(expected))
    assert len(tasks)>1
    assert {tuple(p) for t in tasks for p in t['comparison_pairs']}==expected
    assert all(request_size(SYSTEM,t)<=REQUEST_LIMIT for t in tasks)


def test_shared_concurrency_limit_applies_to_separate_discovery_and_research_calls():
    class Model:
        active=0;peak=0
        async def complete_json(self,*args):
            self.active+=1;self.peak=max(self.peak,self.active)
            await asyncio.sleep(.01)
            self.active-=1
            return {}
    model=Model()
    async def run():
        await asyncio.gather(*(complete(model,'generic',{'tables':[]}) for _ in range(12)))
    asyncio.run(run())
    assert model.peak==3


def test_only_ambiguity_requires_another_model_opinion():
    d={'supported':True,'confidence':.98,'query_outcome':'usable','key_domain':'same',
       'relationship_kind':'direct_record_reference','alternative_unresolved':False}
    c={'evidence':[{'kind':'value_profile','statistics':{'source_nonnull_distinct_keys':5,'directional_coverage':1}}]}
    assert review_reasons(c,d)==[]
    assert 'competing_targets' in review_reasons(c,{**d,'alternative_unresolved':True})
    assert 'shared_endpoint_requires_direct_evidence' in review_reasons({**c,'global_review_reasons':['shared_endpoint_requires_direct_evidence']},d)


def test_identity_and_same_named_business_key_need_role_review_without_being_rejected():
    c = {'source_columns': ['entity_code'], 'target_columns': ['entity_code'],
         'evidence': [{'kind': 'target_key_context', 'primary_key': False}]}
    d = {'supported': True, 'query_outcome': 'usable', 'key_domain': 'same',
         'relationship_kind': 'identity', 'alternative_unresolved': False}
    assert set(review_reasons(c, d)) == {'record_identity_requires_review', 'shared_identifier_requires_role_review'}
    c['evidence'].append({'kind': 'explicit_field_reference'})
    assert review_reasons(c, {**d, 'relationship_kind': 'entity_reference'}) == []


def test_discovery_retries_twice_with_identical_prompt_and_payload():
    class Model:
        def __init__(self):self.calls=[]
        async def complete_json(self,system,user):
            self.calls.append((system,user))
            return {'relations':[],'leads':[],'complete':True} if len(self.calls)==3 else {'leads':[]}
    values=cards(1);model=Model()
    asyncio.run(Discovery(values,model,{},lambda:None).run())
    assert len(model.calls)==3
    assert model.calls[0]==model.calls[1]==model.calls[2]


def test_discovery_stops_after_three_invalid_responses():
    import pytest
    from app.relationship_discovery import DiscoveryIncomplete
    class Model:
        calls=0
        async def complete_json(self,*args):self.calls+=1;return {'leads':[]}
    model=Model()
    with pytest.raises(DiscoveryIncomplete):asyncio.run(Discovery(cards(1),model,{},lambda:None).run())
    assert model.calls==3


def test_output_shape_is_last_in_all_relationship_prompts():
    from app.relationship_discovery import READ_SYSTEM, EXPAND_SYSTEM
    from app.relationship_research import SYSTEM as research, REVIEW_SYSTEM, with_data_guidance
    from app.relationship_output_schema import MARKER
    for prompt in [READ_SYSTEM, EXPAND_SYSTEM, SYSTEM, research, REVIEW_SYSTEM, with_data_guidance(research), with_data_guidance(REVIEW_SYSTEM)]:
        assert prompt.count(MARKER)==1
        schema=json.loads(prompt.split(MARKER)[1].split('\n',1)[1])
        assert schema['type']=='object'
        assert set(schema['required'])<=set(schema['properties'])
        assert 'examples' not in json.dumps(schema)
        assert 'default' not in json.dumps(schema)


def test_extra_visible_pair_does_not_discard_requested_relations():
    from app.relationship_discovery import DiscoveryIncomplete
    import pytest
    source=cards(3);ids=sorted(source);d=Discovery(source,None,{},lambda:None)
    def relation(a,b):return {'source_table_id':a,'target_table_id':b,'key_pairs':[{'source':'id','target':'id'}]}
    p={'phase':'compare','tables':list(source.values()),'comparison_pairs':[[ids[0],ids[1]]]}
    requested=relation(ids[0],ids[1]);extra=relation(ids[0],ids[2])
    result=d.validate_response({'relations':[extra,requested],'leads':[],'complete':True},p)
    assert result['relations']==[extra,requested]
    assert d.metrics['extra_visible_proposals']==1
    with pytest.raises(DiscoveryIncomplete,match='unknown_relation_table'):
        d.validate_response({'relations':[relation(ids[0],'nonexistent')],'leads':[],'complete':True},p)


def test_resume_does_not_repeat_successful_comparison_regions():
    import pytest
    from app.relationship_discovery import DiscoveryIncomplete
    source=cards(40);ids=sorted(source);state={}
    class Model:
        calls=0;fail=True
        async def complete_json(self,system,user):
            self.calls+=1;p=json.loads(user)
            if p['phase']=='read':
                return {'relations':[],'leads':[{'source_table_id':p['tables'][0]['table_id'],'fields':['id'],'question':'引用','keywords':['table']}],'complete':True}
            if p['phase']=='complete_references':
                return {'relations':[],'leads':[{**l,'keywords':[]} for l in p['focus_leads']],'complete':True}
            if self.fail and [ids[0],ids[0]] in p['comparison_pairs']:
                return {'leads':[]}
            return {'relations':[],'leads':[],'complete':True}
    model=Model()
    with pytest.raises(DiscoveryIncomplete):asyncio.run(Discovery(source,model,state,lambda:None).run())
    before=model.calls;model.fail=False
    asyncio.run(Discovery(source,model,state,lambda:None).run())
    assert model.calls==before+3  # failed region, then two source-field completeness requests
    assert state['metrics']['covered_table_pairs']==820
