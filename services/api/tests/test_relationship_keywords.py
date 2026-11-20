"""New pipeline acceptance cases. Real providers and databases are never called here."""
import asyncio
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.relationship_contracts import KeywordOutput
from app.relationship_inputs import build_inputs, Checkpoints, digest
from app.relationship_keywords import KeywordDiscovery, KEYWORD_SYSTEM
from app.relationship_discovery import DiscoveryIncomplete
from app.relationship_research import decode_decision
from app.relationship_work import RelationshipWork, keyword_set


def cards(n):
    return build_inputs({'source':{f's.t{i}':{'id':f's.t{i}','name':f'record_{i}',
        'comment':'对象登记与业务记录','columns':[{'name':'entity_ref','type':'bigint','comment':'对象编号'},
        {'name':'id','type':'bigint','comment':'主键'}], 'primary_keys':[{'columns':['id']}]} for i in range(n)}})


class Keywords:
    def __init__(self): self.calls=[]
    async def complete_json(self,system,user):
        p=json.loads(user); self.calls.append(p)
        assert p['phase']=='keywords'
        return {'keywords':['entity_ref',' ENTITY_REF ','对象']}


def test_first_stage_is_keyword_only_and_batches_commit_before_completion(tmp_path):
    source=cards(41); model=Keywords(); cp=Checkpoints(tmp_path)
    state={'job_id':'job'}; saved=[]
    def save():
        cp.save('job',state); saved.append(json.loads(cp.path('job').read_text()))
    engine=KeywordDiscovery(source,model,state,save)
    result=asyncio.run(engine.extract_keywords())
    assert result==['entity_ref','对象']
    assert len(model.calls)==5 and max(len(p['tables']) for p in model.calls)<=10
    assert all('source_table_id' not in json.dumps(p) for p in model.calls)
    assert 'retrieval_complete' not in engine.scope
    assert any(s.get('keyword_work') for s in saved)
    restored=KeywordDiscovery(source,model,cp.load('job',None),lambda:None)
    # Checkpoints use fingerprints; this standalone fixture intentionally uses no fingerprint.
    assert asyncio.run(restored.extract_keywords())==result
    assert len(model.calls)==5


@pytest.mark.parametrize('word',['id',' ID ','name','status','VARCHAR','字段'])
def test_generic_search_terms_are_rejected_without_rejecting_business_abbreviations(word):
    with pytest.raises(ValueError,match='non_business_search_keyword'): keyword_set([word])
    assert keyword_set(['AB','entity_ref','Entity_Ref'])==['AB','entity_ref']


def test_program_preserves_model_selected_identifier_terms_and_only_normalizes_case_and_spaces():
    assert keyword_set(['candidate_id','CANDIDATE','Candidate_ID','group_im_id'])==['candidate_id','CANDIDATE','group_im_id']
    assert keyword_set(['UID','User ID','user_id','U_ID'])==['UID','User ID','user_id','U_ID']
    assert keyword_set(['entity_id_id','  entity_id_id  '])==['entity_id_id']


def test_chinese_semantic_terms_remain_valid_without_programmatic_character_splitting():
    words=['业务对象','业务对象编号','业务对象','用户IM标识']
    assert keyword_set(words)==['业务对象','业务对象编号','用户IM标识']
    assert '按中英文各自的语义理解名称和注释' in KEYWORD_SYSTEM


def test_model_selected_object_term_retrieves_target_without_programmatic_stemming():
    rows={'s.object':{'id':'s.object','name':'object','columns':[{'name':'id','type':'bigint'}]},
          's.events':{'id':'s.events','name':'events','columns':[{'name':'object_id','type':'bigint'}]}}
    cards=build_inputs({'source':rows});before=json.loads(json.dumps(cards))
    engine=KeywordDiscovery(cards,None,{},lambda:None)
    assert engine.retrieve('OBJECT_ID')['total']==1
    assert engine.retrieve('object')['total']==2
    assert cards==before


def test_model_selected_chinese_object_term_is_saved_and_retrieves_name_independent_target():
    rows={'s.registry':{'id':'s.registry','name':'registry','comment':'业务对象登记',
                       'columns':[{'name':'id','type':'bigint','comment':'主键'}]},
          's.events':{'id':'s.events','name':'events',
                      'columns':[{'name':'owner_ref','type':'varchar(50)','comment':'所属业务对象编号'}]}}
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1
            return {'keywords':['业务对象']}
    model=Model();engine=KeywordDiscovery(build_inputs({'source':rows}),model,{},lambda:None)
    assert asyncio.run(engine.extract_keywords())==['业务对象']
    assert engine.work.keywords()==['业务对象']
    assert engine.retrieve('业务对象')['total']==2
    assert model.calls==1


def test_field_name_and_comment_terms_are_preserved_for_independent_recall():
    rows={
        's.registry':{'id':'s.registry','name':'catalog','columns':[{'name':'id','type':'bigint'}]},
        's.events':{'id':'s.events','name':'events','columns':[
            {'name':'catalog_ref','type':'bigint','comment':'所属目录编号'}]},
        's.history':{'id':'s.history','name':'history','columns':[
            {'name':'owner_ref','type':'bigint','comment':'所属目录编号'}]},
    }
    class Model:
        async def complete_json(self,system,user):
            tables=json.loads(user)['tables']
            assert any(f['name']=='catalog_ref' and f['comment']=='所属目录编号'
                       for table in tables for f in table['columns'])
            assert '逐项检查字段名' in system
            assert '不能只从注释选词' in system
            assert '表名中的业务词或词段、字段名中的业务词或词段' in system
            assert '具有完整业务含义的字段名' in system
            assert '不能仅因语义相同而互相替代' in system
            return {'keywords':['catalog','目录']}
    engine=KeywordDiscovery(build_inputs({'source':rows}),Model(),{},lambda:None)
    assert asyncio.run(engine.extract_keywords())==['catalog','目录']
    hits={r['keyword']:{v['table_id'] for v in r['items']}
          for r in asyncio.run(engine.retrieve_all())}
    assert len(hits['catalog'])==len(hits['目录'])==2
    assert hits['catalog']!=hits['目录']
    assert engine.work.keywords()==['catalog','目录']


def test_keyword_wire_contract_has_no_source_or_explanation():
    assert KeywordOutput.model_validate({'keywords':[]}).keywords==[]
    with pytest.raises(ValidationError): KeywordOutput.model_validate({'keywords':['entity_ref'],'source':'table'})
    assert '禁止输出' in KEYWORD_SYSTEM


def test_input_includes_nonunique_indexes_without_storage_names():
    from app.relationship_keywords import wire_card
    from app.relationship_normalization import unique_key
    table={'id':'s.events','name':'events','columns':[
        {'name':'entity_ref','type':'varchar(40)','comment':'所属对象编号'},
        {'name':'created_at','type':'timestamp','comment':'创建时间'}],
        'indexes':[{'name':'idx_storage_a','columns':['entity_ref','created_at'],'unique':False},
                   {'name':'idx_storage_b','columns':['entity_ref','created_at'],'unique':False}]}
    card=next(iter(build_inputs({'source':{'s.events':table}}).values()))
    assert wire_card(card)['indexes']==[{'columns':['entity_ref','created_at'],'unique':False}]
    assert 'idx_storage' not in json.dumps(wire_card(card))
    assert not unique_key(card,['entity_ref'])
    assert [f['name'] for f in card['columns']]==['entity_ref']


def test_comparison_card_merges_unique_indexes_and_filters_foreign_keys():
    from app.relationship_keywords import comparison_card
    card={'table_id':'a','name':'s.events','columns':[], 'primary_keys':[{'name':'pk','columns':['id']}],
        'unique_constraints':[{'name':'uq','columns':['code']}],
        'indexes':[{'columns':['code'],'unique':True}, {'columns':['other'],'unique':True},
                   {'columns':['ref'],'unique':False}, {'columns':['scoped'],'unique':True,'predicate':'active'}],
        'foreign_keys':[{'columns':['auto'],'target_table':'s.b','target_columns':['id'],'automatic_relation_available':True},
                        {'columns':['pending'],'target_table':'s.b','target_columns':['id'],'automatic_relation_available':False},
                        {'columns':['other'],'target_table':'s.c','target_columns':['id'],'automatic_relation_available':False}]}
    result=comparison_card(card,['s.b'])
    assert 'indexes' not in result
    assert result['primary_keys']==[{'columns':['id']}]
    assert result['unique_constraints']==[{'columns':['code']},{'columns':['other']}]
    assert result['foreign_keys']==[{'columns':['pending'],'target_table':'s.b','target_columns':['id']}]


def test_only_resolvable_complete_foreign_key_mapping_is_marked_automatic():
    target={'id':'s.registry','name':'registry','columns':[{'name':'part_a','type':'bigint'},
        {'name':'part_b','type':'bigint'}]}
    source={'id':'s.events','name':'events','columns':[{'name':n,'type':'varchar(40)'}
        for n in ('entity_a','entity_b','owner_ref')],
        'foreign_keys':[{'columns':['entity_a','entity_b'],'target_table':'s.registry','target_columns':['part_a','part_b']},
                        {'columns':['owner_ref'],'target_table':'s.external','target_columns':['id']}]}
    values=build_inputs({'source':{'s.registry':target,'s.events':source}})
    card=next(c for c in values.values() if c['name']=='events')
    assert [fk['automatic_relation_available'] for fk in card['foreign_keys']]==[True,False]
    assert set(f['name'] for f in card['columns'])=={'entity_a','entity_b','owner_ref'}
    assert 'automatic_relation_available=true' in KEYWORD_SYSTEM


def test_partial_or_expression_index_does_not_prove_global_uniqueness():
    from app.relationship_normalization import unique_key
    assert not unique_key({'indexes':[{'columns':['ref'],'unique':True,'predicate':'active=1'}]},['ref'])
    assert not unique_key({'indexes':[{'columns':['ref'],'unique':True,'expressions':['lower(ref)']}]},['ref'])


def test_search_is_case_insensitive_for_table_field_and_comment():
    values=cards(1);card=next(iter(values.values()))
    card['name']='Object_REGISTRY';card['columns'][0]['comment']='用户IMID'
    engine=KeywordDiscovery(values,None,{},lambda:None)
    for a,b in [('object_registry','OBJECT_REGISTRY'),('entity_ref','ENTITY_REF'),('用户IMID','用户imid')]:
        assert engine.retrieve(a)['items']==engine.retrieve(b)['items']
        assert engine.retrieve(a)['total']==1
    assert len(keyword_set(['sessionId','SESSIONID','session_id']))==2


def test_keyword_prompt_uses_query_style_sections_and_keeps_only_keyword_output():
    from app.relationship_output_schema import MARKER
    sections=['【角色】','【任务】','【输入】','【提取要求】','【依据边界】','【输出】',MARKER]
    positions=[KEYWORD_SYSTEM.index(section) for section in sections]
    assert positions==sorted(positions)
    schema=json.loads(KEYWORD_SYSTEM.split(MARKER,1)[1].split('\n',1)[1])
    assert set(schema['properties'])=={'keywords'}
    assert schema['required']==['keywords'] and schema['additionalProperties'] is False


def test_retrieval_uses_whole_expression_and_caches_all_hits():
    source=cards(107); engine=KeywordDiscovery(source,None,{},lambda:None)
    first=engine.retrieve('entity_ref')
    assert first['total']==107 and len(first['items'])==107
    source[next(iter(source))]['columns']=[]
    assert engine.retrieve('ENTITY_REF')==first  # same immutable input/version cache
    with pytest.raises(ValueError,match='non_business_search_keyword'):
        engine.retrieve('column')
    assert engine.retrieve('columns')['total']==0  # JSON attribute names are not searched
    assert engine.retrieve('对象 编号')['total']==106  # whitespace is ignored
    assert engine.retrieve('对象 不存在')['total']==0  # still not split into a broad OR


def test_later_stages_do_not_implicitly_call_the_keyword_model():
    model=Keywords(); engine=KeywordDiscovery(cards(2),model,{},lambda:None)
    with pytest.raises(DiscoveryIncomplete,match='keyword_stage_not_completed'):
        asyncio.run(engine.retrieve_all())
    with pytest.raises(DiscoveryIncomplete,match='retrieval_stage_not_completed'):
        asyncio.run(engine.discover())
    assert not model.calls


def test_small_groups_keep_comparison_and_large_groups_use_agent(monkeypatch):
    class Model:
        def __init__(self): self.phases=[]
        async def complete_json(self,system,user):
            p=json.loads(user); self.phases.append(p['phase'])
            if p['phase']=='keywords': return {'keywords':['entity_ref']}
            if p['phase']=='organization': return {'groups':[{'table_ids':g['table_ids'], 'reason':'编号体系一致，保留待分析'} for g in p['groups']], 'excluded':[]}
            if p['phase']=='compare': return {'relations':[]}
            if p['phase']=='large_group_agent':
                return {'tool':'finish','arguments':{'checked_table_ids':[c['table_id'] for c in p['catalog']['items']], 'gaps':[]}}
            raise AssertionError(p['phase'])
    monkeypatch.setenv('RELATIONSHIP_AGENT_GROUP_THRESHOLD','3')
    for count,phase in [(3,'compare'),(4,'large_group_agent')]:
        m=Model(); asyncio.run(KeywordDiscovery(cards(count),m,{},lambda:None).run())
        assert phase in m.phases
        assert 'organization' not in m.phases
        assert ('compare' in m.phases) != ('large_group_agent' in m.phases)


def test_small_groups_skip_organization_and_large_groups_organize_one_request():
    class Model:
        def __init__(self):self.phases=[]
        async def complete_json(self,system,user):
            p=json.loads(user);self.phases.append(p['phase'])
            if p['phase']=='keywords':return {'keywords':['entity_ref']}
            if p['phase']=='organization':
                assert len(p['groups'])==1
                return {'groups':[{'table_ids':g['table_ids'],
                                   'reason':'整组保留分析'} for g in p['groups']]}
            if p['phase']=='compare':return {'relations':[]}
            raise AssertionError(p['phase'])
    small=Model();asyncio.run(KeywordDiscovery(cards(2),small,{},lambda:None).run())
    assert small.phases==['keywords','compare']
    large=Model();asyncio.run(KeywordDiscovery(cards(21),large,{},lambda:None).run())
    assert large.phases[0]=='keywords'
    assert 'organization' in large.phases and 'compare' in large.phases
    assert large.phases.index('organization')<large.phases.index('compare')


def test_compact_compare_does_not_expand_keywords_or_gaps():
    class Model:
        async def complete_json(self,system,user):
            p=json.loads(user)
            if p['phase']=='keywords':return {'keywords':['entity_ref']}
            if p['phase']=='compare':return {'relations':[],'keywords':['record'],'gaps':['obsolete']}
            raise AssertionError(p['phase'])
    engine=KeywordDiscovery(cards(2),Model(),{},lambda:None)
    asyncio.run(engine.run())
    assert engine.work.keywords()==['entity_ref']
    assert 'gaps' not in engine.scope


def test_stalled_agent_switches_to_conclusion_and_records_unread_scope():
    class Repeat:
        calls=[]
        async def complete_json(self,system,user):
            p=json.loads(user);self.calls.append(p)
            if p['phase']=='agent_conclusion':
                assert '不提供搜索、读取或取证工具' in system
                return {'relations':[],'checked_table_ids':[],'gaps':['尚有结构未读完']}
            return {'tool':'catalog','arguments':{'page':0}}
    engine=KeywordDiscovery(cards(41),Repeat(),{},lambda:None)
    ids=tuple(sorted(engine.cards))
    assert asyncio.run(engine.agent(ids,['entity_ref']))==[]
    context=next(iter(engine.scope['agents'].values()))
    assert context['complete'] and context['step']==4
    assert len(context['unresolved_table_ids'])==41
    assert not engine.incomplete_units()


def test_repeated_read_action_automatically_continues_instead_of_replaying_first_tables():
    source=cards(2);ids=sorted(source)
    for card in source.values():
        card['columns'] += [{'name':f'attribute_{i}','type':'varchar(40)','comment':'记录编号说明'*12} for i in range(50)]
    class Model:
        calls=[]
        async def complete_json(self,system,user):
            payload=json.loads(user);self.calls.append(payload)
            if payload['progress']['remaining']['table_count']:
                return {'tool':'read_tables','arguments':{'table_ids':ids}}
            return {'tool':'finish','arguments':{'checked_table_ids':ids,'gaps':[]}}
    engine=KeywordDiscovery(source,Model(),{},lambda:None)
    assert asyncio.run(engine.agent(tuple(ids),['entity_ref']))==[]
    context=engine.scope['agents'][digest(tuple(ids))]
    assert context['complete'] and context['step']<=10 and context.get('correction_rounds',0)==0
    assert all(len(context['read'][t])==52 for t in ids)
    seen=set()
    for action in context['history']:
        result=action['result']
        assert len(json.dumps(result,ensure_ascii=False).encode())<=18000
        for table in result.get('tables',[]):
            for field in table['columns']:
                key=(table['table_id'],field['name'])
                assert key not in seen
                seen.add(key)
    assert len(seen)==104


def test_table_read_skips_complete_tables_but_allows_explicit_reread():
    source=cards(2);ids=sorted(source);engine=KeywordDiscovery(source,None,{},lambda:None)
    context={'seen':[],'read':{ids[0]:[0,1]}}
    async def run():
        result=await engine.agent_tool('read_tables',{'table_ids':ids},context,ids,[])
        assert [c['table_id'] for c in result['tables']]==[ids[1]]
        assert result['skipped_read_table_ids']==[ids[0]] and result['next_reads']==[]
        replay=await engine.agent_tool('read_tables',{'table_ids':[ids[0]],'reread':True},context,ids,[])
        assert len(replay['tables'][0]['columns'])==2
    asyncio.run(run())


def test_twenty_wide_tables_return_bounded_continuations_and_fill_legacy_read_gaps():
    source=cards(20);ids=sorted(source)
    for card in source.values():
        card['columns']=[{'name':f'field_{i}','type':'varchar(40)','comment':'字段用途说明'*15} for i in range(40)]
    engine=KeywordDiscovery(source,None,{},lambda:None)
    context={'seen':[],'read':{ids[0]:list(range(20,40))}}
    async def run():
        results=[]
        for _ in range(40):
            result=await engine.agent_tool('read_tables',{'table_ids':ids},context,ids,[])
            assert len(json.dumps(result,ensure_ascii=False).encode())<=18000
            results.extend(result['tables'])
            if not result['next_reads']:break
        assert engine.agent_remaining(ids,{**context,'checked':[]})['table_count']==0
        assert all(set(context['read'][t])==set(range(40)) for t in ids)
        first=[r for r in results if r['table_id']==ids[0]]
        assert all(int(f['name'].split('_')[-1])<20 for row in first for f in row['columns'])
        assert first[-1]['has_more'] is False and first[-1]['next_offset']==40
    asyncio.run(run())


def test_five_exploration_calls_then_at_most_three_conclusion_attempts(monkeypatch):
    monkeypatch.setenv('RELATIONSHIP_AGENT_MAX_STEPS','80')
    source=cards(12);ids=sorted(source)
    class Model:
        phases=[]
        async def complete_json(self,system,user):
            p=json.loads(user);self.phases.append(p['phase'])
            if p['phase']=='agent_conclusion':return {'wrong':True}
            return {'tool':'read_tables','arguments':{'table_ids':[ids[len(self.phases)-1]]}}
    engine=KeywordDiscovery(source,Model(),{},lambda:None)
    with pytest.raises(DiscoveryIncomplete,match='agent_conclusion_failed'):
        asyncio.run(engine.agent(tuple(ids),['entity_ref']))
    assert engine.model.phases==['large_group_agent']*5+['agent_conclusion']*3


def test_conclusion_keeps_previously_submitted_relation_without_requiring_repetition():
    source=cards(2);ids=sorted(source)
    relation={'source_table_id':ids[0],'target_table_id':ids[1],
        'key_pairs':[{'source':'entity_ref','target':'id'}],'guards':[],'reason':'引用登记对象'}
    class Model:
        async def complete_json(self,system,user):
            p=json.loads(user)
            assert p['phase']=='agent_conclusion' and p['submitted_count']==1
            return {'relations':[],'checked_table_ids':ids,'gaps':[]}
    engine=KeywordDiscovery(source,Model(),{},lambda:None)
    context={'step':5,'relations':{engine.proposal_key(relation):relation},
        'history':[],'read':{t:[0,1] for t in ids},'checked':[]}
    assert asyncio.run(engine.conclude_agent(context,ids,['entity_ref']))==[relation]
    assert context['step']==6 and context['complete']


def test_partial_table_read_and_one_invalid_mapping_do_not_reject_good_conclusion():
    source=cards(2);ids=sorted(source)
    good={'source_table_id':ids[0],'target_table_id':ids[1],
        'key_pairs':[{'source':'entity_ref','target':'id'}],'reason':'正常关联说明'*100}
    bad={**good,'key_pairs':[{'source':'entity_ref','target':'missing'}]}
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1
            return {'relations':[good,bad],'checked_table_ids':ids,'gaps':[]}
    engine=KeywordDiscovery(source,Model(),{},lambda:None)
    context={'step':5,'relations':{},'history':[],
        'read':{ids[0]:[0],ids[1]:[1]},'checked':[]}
    submitted=asyncio.run(engine.agent_tool('submit',{'relations':[good]},context,ids,[]))
    assert submitted['saved']==1 and not submitted['rejected']
    result=asyncio.run(engine.conclude_agent(context,ids,['entity_ref']))
    assert len(result)==1 and result[0]['reason']==good['reason']
    assert engine.model.calls==1 and context['complete']
    assert set(context['unresolved_table_ids'])==set(ids) and context['gaps']
    assert not engine.incomplete_units()


def test_many_small_groups_remain_independent_even_above_global_task_threshold():
    source=cards(64);ids=sorted(source);groups={tuple(ids[i:i+2]):['entity_ref'] for i in range(0,64,2)}
    class Model:
        calls=[]
        async def complete_json(self,system,user):
            p=json.loads(user);self.calls.append(p)
            assert p['phase']=='compare'
            assert tuple(sorted(t['table_id'] for t in p['tables'])) in groups
            return {'relations':[]}
    engine=KeywordDiscovery(source,Model(),{},lambda:None)
    engine.finish_stage('retrieval',{'counts':{}})
    async def arranged():return groups
    engine.arranged_groups=arranged
    assert asyncio.run(engine.discover())==[]
    assert len(engine.model.calls)==32 and set(engine.scope['group_routes'].values())=={'comparison'}


def test_context_deduplicates_fields_and_trims_oversized_history():
    from app.relationship_keywords import AGENT_SYSTEM
    from app.relationship_context import request_size,REQUEST_LIMIT
    source=cards(2);engine=KeywordDiscovery(source,None,{},lambda:None);card=next(iter(source.values()))
    payload={'recent_results':[{'tool':'read_tables','result':{'tables':[card]}}]*3,
             'catalog':{'items':[{'table_id':card['table_id']}],'has_more':False}}
    fitted=engine.fit_agent_payload(AGENT_SYSTEM,payload)
    columns=[f for r in fitted['recent_results'] for t in r['result'].get('tables',[]) for f in t['columns']]
    assert len(columns)==len(card['columns']) and fitted['catalog']['items']==[]
    payload['recent_results'].append({'tool':'definition','result':{'definition':'x'*100000}})
    fitted=engine.fit_agent_payload(AGENT_SYSTEM,payload)
    assert request_size(AGENT_SYSTEM,fitted)<=REQUEST_LIMIT and fitted['context_trimmed']
    huge={**card,'comment':'x'*50000,'matched_fields':card['columns']*10000}
    summary=engine.page([huge])
    assert summary['items'][0]['summary_omitted']


def test_early_finish_reports_missing_tables_without_marking_complete():
    engine=KeywordDiscovery(cards(2),None,{},lambda:None); ids=list(engine.cards)
    context={'seen':[ids[0]],'checked':[],'gaps':[],'relations':{}}
    result=asyncio.run(engine.agent_tool('finish',{'checked_table_ids':[ids[0]],'gaps':[]},context,ids,[]))
    assert result['error']=='unaccounted_group_tables' and not context.get('complete')


def test_database_claim_and_batch_commit_use_atomic_conflicts_and_owner_fencing():
    connection=Mock(); connection.__enter__=Mock(return_value=connection); connection.__exit__=Mock(return_value=False)
    transaction=Mock(); transaction.__enter__=Mock(); transaction.__exit__=Mock(return_value=False)
    connection.transaction.return_value=transaction
    connection.execute.return_value.fetchone.return_value=('claimed',)
    repo=Mock(dsn='postgres'); repo._connect.return_value=connection
    work=RelationshipWork(repo,'job','hash',{},lambda:None)
    assert work.claim('keywords','batch')
    work.finish('keywords','batch',{'keywords':['entity_ref']},['entity_ref'])
    statements=[c.args[0] for c in connection.execute.call_args_list]
    assert 'ON CONFLICT' in statements[0] and 'lease_until<now()' in statements[0]
    assert 'owner=%s' in statements[1] and "status='running'" in statements[1]
    assert 'ON CONFLICT' in statements[2]
    assert connection.transaction.call_count==1


def test_compact_decision_expands_internal_flags_and_keeps_conditions():
    predicate={'side':'source','field':'kind','operator':'eq','value':2}
    raw={'candidate_id':'c','outcome':'usable','kind':'entity_reference','reason':'来源对象引用目标登记'}
    result=decode_decision(raw,[predicate])
    assert result['supported'] and result['key_domain']=='same' and result['structure_sufficient']
    assert result['additional_predicates']==[predicate]
    assert decode_decision({**raw,'predicates':[]},[predicate])['additional_predicates']==[]
    assert decode_decision({**raw,'kind':'shared_entity'}) is None
    assert decode_decision({**raw,'gaps':['编号体系未确定']}) is None
    assert decode_decision({**raw,'coverage':'identifier_conflict'}) is None


def test_review_schema_does_not_request_redundant_internal_flags():
    from app.relationship_output_schema import RESEARCH_SCHEMA
    properties=RESEARCH_SCHEMA['$defs']['CompactDecision']['properties']
    assert set(properties).isdisjoint({'supported','structure_sufficient','target_role',
        'alternative_unresolved','key_domain','query_role','coverage_expectation'})


def test_inflight_evidence_is_shared_across_agent_groups():
    calls=[]
    async def tool(kind,args):
        calls.append(kind)
        await asyncio.sleep(0)
        return {'statistics':{'matched_keys':2,'guards_applied':[]}}
    engine=KeywordDiscovery(cards(2),None,{},lambda:None,tool=tool)
    async def run():
        first,second=await asyncio.gather(engine.evidence('profile','same',{}),engine.evidence('profile','same',{}))
        assert first==second
        assert await engine.evidence('profile','same',{})==first
    asyncio.run(run())
    assert calls==['profile']


def test_small_agent_roundtrip_preserves_full_mapping_guards_and_statistics(monkeypatch):
    source=cards(2); ids=list(source)
    source[ids[0]]['columns'].append({'name':'kind','type':'integer','comment':'1:主体；2:其他'})
    relation={'source_table_id':ids[0],'target_table_id':ids[1],
        'key_pairs':[{'source':'entity_ref','target':'id'}],
        'guards':[{'side':'source','field':'kind','operator':'eq','value':1}],
        'reason':'来源编号引用目标登记记录'}
    class Model:
        step=0
        calls=[]
        async def complete_json(self,system,user):
            p=json.loads(user); self.calls.append(p['phase'])
            if p['phase']=='keywords': return {'keywords':['entity_ref']}
            if p['phase']=='organization': return {'groups':[{'table_ids':g['table_ids'], 'reason':'编号体系一致，保留待分析'} for g in p['groups']], 'excluded':[]}
            actions=[{'tool':'read_tables','arguments':{'table_ids':ids}},
                     {'tool':'profile','arguments':{'relation':relation}},
                     {'tool':'submit','arguments':{'relations':[relation]}},
                     {'tool':'finish','arguments':{'checked_table_ids':ids,'gaps':[]}}]
            result=actions[self.step]; self.step+=1
            return result
    async def tool(kind,args):
        assert kind=='profile' and args['relation']['guards']==relation['guards']
        return {'statistics':{'matched_keys':2,'directional_coverage':1,'guards_applied':relation['guards']}}
    monkeypatch.setenv('RELATIONSHIP_AGENT_GROUP_THRESHOLD','1')
    model=Model(); state={}; engine=KeywordDiscovery(source,model,state,lambda:None,tool=tool)
    result=asyncio.run(engine.run())
    assert len(result)==1
    assert result[0]['key_pairs']==relation['key_pairs'] and result[0]['guards']==relation['guards']
    assert result[0]['profile_statistics']['matched_keys']==2
    assert model.calls==['keywords']+['large_group_agent']*4
    assert engine.scope['discovery_complete']


def test_search_ignores_all_whitespace_in_both_keyword_and_original_metadata():
    source=cards(2)
    for c in source.values():
        c['columns'][0]['comment']='业务会话 ID'
    engine=KeywordDiscovery(source,None,{},lambda:None)
    compact=engine.retrieve('业务会话ID')
    assert compact['total']==2
    assert engine.retrieve('业务 会话\tID')==compact
    assert len([key for key in engine.work.local if key.startswith('retrieval:')])==1
    assert len(keyword_set(['业务会话ID','业务 会话 ID']))==1


def test_retrieval_prunes_single_hits_atomically_and_survives_restart():
    source=cards(2);state={}
    engine=KeywordDiscovery(source,None,state,lambda:None)
    words=['entity_ref','record_0']
    assert engine.work.claim('keywords','batch')
    engine.work.finish('keywords','batch',{'keywords':words},words)
    engine.finish_stage('keywords',{'keyword_count':2})
    results=asyncio.run(engine.retrieve_all())
    assert [r['total'] for r in results]==[2,1]
    assert engine.work.keywords()==['entity_ref']
    assert engine.stage_result('retrieval')['counts']=={'entity_ref':2,'record_0':1}
    assert engine.work.get('keywords','batch')['payload']['keywords']==words
    restored=KeywordDiscovery(source,None,state,lambda:None)
    assert asyncio.run(restored.retrieve_all())==results
    assert restored.work.keywords()==['entity_ref']
    restored.add_keywords(['record_0','missing_object','对象'])
    assert restored.work.keywords()==['entity_ref','对象']
    assert all(len(ids)>=2 for ids in restored.groups())


def test_zero_hit_is_logged_and_removed_without_failing_retrieval():
    engine=KeywordDiscovery(cards(2),None,{},lambda:None)
    engine.work.claim('keywords','old')
    engine.work.finish('keywords','old',{'keywords':['missing_object']},['missing_object'])
    engine.finish_stage('keywords',{'keyword_count':1})
    result=asyncio.run(engine.retrieve_all())
    assert result[0]['total']==0
    assert engine.stage_result('retrieval')['counts']=={'missing_object':0}
    assert engine.work.keywords()==[]
    assert engine.incomplete_units()==[]
    assert engine.scope['zero_hit_keywords']==['missing_object']


def test_failed_keyword_batch_does_not_cancel_siblings_and_resume_reuses_success():
    class Model:
        def __init__(self):self.calls=[];self.broken=True
        async def complete_json(self,system,user):
            tables=json.loads(user)['tables'];key=tables[0]['table_id'];self.calls.append(key)
            if self.broken and key==bad:raise TimeoutError('model timeout')
            return {'keywords':['entity_ref']}
    model=Model();state={};engine=KeywordDiscovery(cards(21),model,state,lambda:None)
    bad=list(engine.keyword_batches())[0][0]['table_id']
    assert asyncio.run(engine.extract_keywords())==['entity_ref']
    assert engine.scope['keywords_complete'] is False
    assert len(engine.incomplete_units())==1
    completed_calls={key:model.calls.count(key) for key in model.calls if key!=bad}
    model.broken=False
    resumed=KeywordDiscovery(engine.cards,model,state,lambda:None)
    assert asyncio.run(resumed.extract_keywords())==['entity_ref']
    assert not resumed.incomplete_units() and resumed.stage_result('keywords')['complete']
    assert all(model.calls.count(key)==count for key,count in completed_calls.items())


def test_parallel_waves_preserve_earlier_gaps_and_storage_failures_abort():
    engine=KeywordDiscovery(cards(2),None,{},lambda:None)
    async def operation(value):
        if value=='bad':raise TimeoutError('timeout')
        if value=='network':raise ConnectionError('connection reset')
        return value
    async def run():
        assert await engine.parallel_work(['ok','bad'],'comparing',operation)==['ok',None]
        assert await engine.parallel_work(['later'],'comparing',operation)==['later']
        assert len(engine.incomplete_units())==1
        assert await engine.parallel_work(['network','healthy'],'comparing',operation)==[None,'healthy']
        assert len(engine.incomplete_units())==2
        async def storage(value):raise OSError('disk full')
        with pytest.raises(OSError,match='disk full'):
            await engine.parallel_work(['write'],'writing',storage)
    asyncio.run(run())


def test_oversized_table_is_an_explicit_gap_and_other_inputs_continue():
    engine=KeywordDiscovery(cards(2),Keywords(),{},lambda:None)
    original=engine.fragments;bad=sorted(engine.cards)[0]
    def fragments(card):
        if card['table_id']==bad:raise DiscoveryIncomplete('field_oversized')
        return original(card)
    engine.fragments=fragments
    assert asyncio.run(engine.extract_keywords())==['entity_ref','对象']
    assert engine.incomplete_units()[0]['table_ids']==[bad]
    assert engine.stage_result('keywords')['complete'] is False


def test_stalled_agent_keeps_submitted_proposals_and_resumes_only_incomplete_group(monkeypatch):
    monkeypatch.setenv('RELATIONSHIP_AGENT_GROUP_THRESHOLD','1')
    state={};engine=KeywordDiscovery(cards(4),None,state,lambda:None)
    ids=sorted(engine.cards);groups={tuple(ids[:2]):['entity_ref'],tuple(ids[2:]):['对象']}
    engine.finish_stage('retrieval',{'counts':{}})
    async def arranged():return groups
    engine.arranged_groups=arranged
    def relation(group):return {'source_table_id':group[0],'target_table_id':group[1],
        'key_pairs':[{'source':'entity_ref','target':'id'}],'reason':'引用对象登记','guards':[]}
    async def agent(group,words):
        r=relation(group)
        if group==tuple(ids[:2]):
            engine.scope.setdefault('agents',{})[digest(group)]={'relations':{engine.proposal_key(r):r}}
            raise DiscoveryIncomplete('agent_no_progress')
        return [r]
    engine.agent=agent
    result=asyncio.run(engine.discover())
    assert len(result)==2 and len(engine.incomplete_units())==1
    assert engine.stage_result('discovery')['complete'] is False
    assert engine.scope['analyzed_groups']==[digest(tuple(ids[2:]))]
    resumed=KeywordDiscovery(engine.cards,None,state,lambda:None);called=[]
    resumed.arranged_groups=arranged
    async def retry(group,words):called.append(group);return [relation(group)]
    resumed.agent=retry
    assert len(asyncio.run(resumed.discover()))==2
    assert called==[tuple(ids[:2])] and not resumed.incomplete_units()
    assert resumed.stage_result('discovery')['complete'] is True


def test_agent_tool_timeout_is_feedback_and_successful_retry_clears_gap():
    source=cards(2);ids=sorted(source);calls=[]
    class Model:
        async def complete_json(self,system,user):
            if len(calls)<2:return {'tool':'definition','arguments':{'table_id':ids[0]}}
            return {'tool':'finish','arguments':{'checked_table_ids':ids,'gaps':[]}}
    async def tool(kind,args):
        calls.append(kind)
        if len(calls)==1:raise TimeoutError('temporary definition failure')
        return {'definition':'available'}
    engine=KeywordDiscovery(source,Model(),{},lambda:None,tool=tool)
    assert asyncio.run(engine.agent(tuple(ids),['entity_ref']))==[]
    context=engine.scope['agents'][digest(tuple(ids))]
    assert len(calls)==2 and not context['tool_failures'] and context['complete']


def test_invalid_keyword_is_ignored_without_retrying_or_failing_the_batch():
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1
            return {'keywords':['概括改写后的不存在对象']}
    model=Model();engine=KeywordDiscovery(cards(2),model,{},lambda:None)
    assert asyncio.run(engine.extract_keywords())==[]
    assert model.calls==1 and not engine.work.keywords()
    assert engine.scope['keywords_complete'] is True
    rejected=next(iter(engine.scope['keyword_discards'].values()))
    assert rejected[0]['keyword']=='概括改写后的不存在对象'
    assert rejected[0]['reason']=='keyword_without_source_match'


def test_mixed_keywords_keep_valid_terms_without_retry_and_log_each_rejection(monkeypatch):
    import logging
    events=[]
    monkeypatch.setattr(logging.getLogger('semora.model'),'info',lambda message:events.append(json.loads(message)))
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1
            assert set(json.loads(user))=={'phase','tables'}
            return {'keywords':[' ENTITY_REF ','entity_ref','对象 编号','对象 编号','ID','\t ',
                                '没有来源的词',42,{'not':'a keyword'}]}
    source=cards(2);model=Model();state={'job_id':'test-job'}
    engine=KeywordDiscovery(source,model,state,lambda:None)
    expected=['ENTITY_REF','对象 编号']
    assert asyncio.run(engine.extract_keywords())==expected
    assert model.calls==1 and engine.metrics['discarded_keywords']==5
    discarded=next(e for e in events if e['event']=='relationship.keywords.discarded')
    assert discarded['job_id']=='test-job' and discarded['batch_index']==1 and discarded['batch_total']==1
    assert discarded['table_count']==2 and discarded['datasource_ids']==['source']
    assert {t['name'] for t in discarded['tables']}=={'s.t0','s.t1'}
    assert discarded['retry'] is False and discarded['normalization']=='casefold_remove_whitespace'
    assert [item['reason'] for item in discarded['discarded']]==[
        'non_business_search_keyword','non_business_search_keyword','keyword_without_source_match',
        'invalid_keyword_type','invalid_keyword_type']
    missing=next(item for item in discarded['discarded'] if item.get('keyword')=='没有来源的词')
    assert missing['normalized_keyword']=='没有来源的词'
    completed=next(e for e in events if e['event']=='relationship.request.completed')
    assert completed['accepted_count']==2 and completed['duplicate_count']==2
    assert not any(e['event']=='relationship.compact_response.invalid' for e in events)
    restored=KeywordDiscovery(source,model,state,lambda:None)
    assert asyncio.run(restored.extract_keywords())==expected
    assert model.calls==1


def test_bad_output_envelope_still_retries_and_preserves_diagnostic_context(monkeypatch):
    import logging
    events=[]
    monkeypatch.setattr(logging.getLogger('semora.model'),'info',lambda message:events.append(json.loads(message)))
    class Model:
        calls=0
        async def complete_json(self,system,user):self.calls+=1;return {'wrong_field':[]}
    model=Model();state={'job_id':'failed-job'}
    engine=KeywordDiscovery(cards(1),model,state,lambda:None)
    assert asyncio.run(engine.extract_keywords())==[]
    assert engine.stage_result('keywords')['complete'] is False
    assert engine.incomplete_units()[0]['stage']=='extracting_keywords'
    assert model.calls==3
    failures=[e for e in events if e['event']=='relationship.compact_response.invalid']
    assert [e['attempt'] for e in failures]==[1,2,3]
    assert [e['will_retry'] for e in failures]==[True,True,False]
    assert all(e['job_id']=='failed-job' and e['batch_index']==1 and e['tables'] for e in failures)
    assert all(e['details']['validation'] and e['traceback'] for e in failures)
    key=failures[-1]['request_key']
    persisted=engine.work.get('keywords',key)['payload']['diagnostic']
    assert persisted['job_id']=='failed-job' and persisted['attempt']==3
    assert persisted['error_code']=='invalid_output_contract'


def test_invalid_relation_is_ignored_without_retrying_valid_siblings():
    from app.relationship_contracts import ProposalOutput
    from app.relationship_keywords import COMPARE_SYSTEM
    source=cards(4);ids=list(source)
    valid={'source_table_id':ids[0],'target_table_id':ids[1],
           'key_pairs':[{'source':'entity_ref','target':'id'}],'reason':'合法引用'}
    invalid={'source_table_id':ids[2],'target_table_id':ids[3],
             'key_pairs':[{'source':'entity_ref','target':'missing_field'}],'reason':'非法字段'}
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1
            if len(json.loads(user)['tables'])==2:
                return {'relations':[invalid]}
            return {'relations':[valid,invalid]}
    model=Model();state={};engine=KeywordDiscovery(source,model,state,lambda:None)
    result=asyncio.run(engine.request('compare',COMPARE_SYSTEM,{
        'phase':'compare','tables':list(source.values()),
        'comparison_pairs':[[ids[0],ids[1]],[ids[2],ids[3]]]},ProposalOutput))
    assert result['relations']==[{**valid,'guards':[]}]
    assert model.calls==1
    assert engine.metrics['discarded_relations']>=1
    discarded=engine.scope['discarded_relations'][0]
    assert discarded['code']=='unknown_relation_field'
    assert discarded['details']['source_table_id']==ids[2]
    assert discarded['details']['fields']==[{'side':'target','field':'missing_field'}]


def test_unknown_field_is_ignored_without_extra_model_calls():
    from app.relationship_contracts import ProposalOutput
    from app.relationship_keywords import COMPARE_SYSTEM
    values=cards(4);ids=list(values)
    corrected={'source_table_id':ids[0],'target_table_id':ids[1],
               'key_pairs':[{'source':'entity_ref','target':'id'}],'reason':'对象引用唯一标识'}
    class Model:
        sizes=[]
        async def complete_json(self,system,user):
            p=json.loads(user);self.sizes.append(len(p['tables']))
            if len(p['tables'])==2:return {'relations':[corrected]}
            return {'relations':[{**corrected,'key_pairs':[{'source':'entity_ref','target':'absent'}]}]}
    model=Model();engine=KeywordDiscovery(values,model,{},lambda:None)
    result=asyncio.run(engine.request('compare',COMPARE_SYSTEM,{'phase':'compare','tables':list(values.values()),
        'comparison_pairs':[[ids[0],ids[1]]]},ProposalOutput))
    assert result['relations']==[]
    assert model.sizes==[4]


def test_valid_fields_cannot_escape_requested_comparison_pair():
    values=cards(3);ids=list(values);engine=KeywordDiscovery(values,None,{},lambda:None)
    result,bad=engine.valid_proposals({'relations':[{'source_table_id':ids[0],'target_table_id':ids[2],
        'key_pairs':[{'source':'entity_ref','target':'id'}],'reason':'未要求的表对'}]},list(values.values()),[[ids[0],ids[1]]])
    assert result['relations']==[] and bad[0]['code']=='unrequested_relation_pair'


def test_malformed_relation_contract_does_not_invalidate_other_items():
    from app.relationship_contracts import ProposalOutput
    from app.relationship_keywords import COMPARE_SYSTEM
    source=cards(2);ids=list(source)
    valid={'source_table_id':ids[0],'target_table_id':ids[1],
           'key_pairs':[{'source':'entity_ref','target':'id'}],'reason':'合法引用'}
    class Model:
        async def complete_json(self,system,user):
            return {'relations':[valid,{'source_table_id':ids[0]}]}
    engine=KeywordDiscovery(source,Model(),{},lambda:None)
    result=asyncio.run(engine.request('compare',COMPARE_SYSTEM,{
        'phase':'compare','tables':list(source.values()),
        'comparison_pairs':[[ids[0],ids[1]]]},ProposalOutput))
    assert result['relations']==[{**valid,'guards':[]}]
    assert engine.scope['discarded_relations'][0]['validation']
