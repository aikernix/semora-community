import asyncio
import json
from types import SimpleNamespace

import pytest

from app.cross_source_discovery import CrossSourceDiscovery, CROSS_COMPARE_SYSTEM, load_stored_keyword_records
from app.datasource_modeling import (
    ModelingRepository, ModelingService, _candidate, convert_model_candidates, tables_digest,
)
from app.relationship_discovery import DiscoveryIncomplete
from app.relationship_inputs import build_inputs
from app.relationship_keywords import COMPARE_SYSTEM, wire_card


def fixture():
    tables = {
        'a': {
            'public.orders': {'id':'public.orders','name':'orders','comment':'订单记录','columns':[
                {'name':'id','type':'bigint','comment':'订单主键'},
                {'name':'customer_id','type':'bigint','comment':'客户编号'}]},
            'public.invoice': {'id':'public.invoice','name':'invoice','comment':'发票记录','columns':[
                {'name':'id','type':'bigint','comment':'发票主键'},
                {'name':'customer_id','type':'bigint','comment':'客户编号'}]},
        },
        'b': {
            'crm.customer': {'id':'crm.customer','name':'customer','comment':'客户主档','columns':[
                {'name':'id','type':'bigint','comment':'客户编号'}], 'primary_keys':[{'columns':['id']}]},
            'crm.account': {'id':'crm.account','name':'account','comment':'账户主档','columns':[
                {'name':'id','type':'bigint','comment':'客户编号'}], 'primary_keys':[{'columns':['id']}]},
        },
    }
    return tables, build_inputs({source: values for source, values in tables.items()})


def engine_for(cards, records, model=None):
    state = {'job_id':'cross-job'}
    return CrossSourceDiscovery(cards, model, state, lambda:None, repository=SimpleNamespace(dsn=''),
        left_source_id='a', right_source_id='b', keyword_records=records), state


def test_single_table_keywords_are_reused_and_single_sided_hits_are_excluded():
    tables, cards = fixture()
    a_order = next(key for key, card in cards.items() if card['physical_table']=='public.orders')
    b_customer = next(key for key, card in cards.items() if card['physical_table']=='crm.customer')
    records = {
        'a': {
            '客户编号': {'keyword':'客户编号','items':[{'table_id':a_order,'matches':['columns.customer_id.comment']}]},
            '客户': {'keyword':'客户','items':[{'table_id':a_order,'matches':['columns.customer_id.comment']}]},
            '订单': {'keyword':'订单','items':[{'table_id':a_order,'matches':['table.comment']}]},
        },
        'b': {
            '客户编号': {'keyword':'客户编号','items':[{'table_id':b_customer,'matches':['columns.id.comment']}]},
            '客户': {'keyword':'客户','items':[{'table_id':b_customer,'matches':['table.comment']}]},
            '订单': {'keyword':'订单','items':[]},
        },
    }
    engine, _ = engine_for(cards, records)
    pairs = engine.recall()
    assert list(pairs) == [(a_order, b_customer)]
    assert len(pairs[(a_order, b_customer)]['hits']) == 2
    assert engine.summary['union_keyword_count'] == 3
    assert engine.summary['shared_keyword_count'] == 2
    assert engine.summary['cross_candidate_pairs'] == 1


def test_missing_other_side_hit_is_computed_only_from_saved_source_cards():
    tables, cards = fixture()
    a_order = next(key for key, card in cards.items() if card['physical_table']=='public.orders')
    b_customer = next(key for key, card in cards.items() if card['physical_table']=='crm.customer')
    records = {
        'a': {'客户编号': {'keyword':'客户编号','items':[{'table_id':a_order,'matches':[]}]}},
        'b': {},
    }
    engine, _ = engine_for(cards, records)
    result = engine.retrieve_side('b', '客户编号')
    assert result['total'] == 2  # customer.id and account.id both contain the stored comment.
    assert {item['table_id'] for item in result['items']} == {
        b_customer, next(key for key, card in cards.items() if card['physical_table']=='crm.account')}


def test_cross_prompt_has_two_source_groups_and_never_changes_single_source_prompt():
    tables, cards = fixture()
    records = {
        'a': {'客户编号': {'keyword':'客户编号','items':None}},
        'b': {'客户编号': {'keyword':'客户编号','items':None}},
    }
    engine, _ = engine_for(cards, records)
    engine.recall()
    payload = next(iter(engine.cross_regions()))
    assert CROSS_COMPARE_SYSTEM != COMPARE_SYSTEM
    assert 'tables' not in payload
    assert payload['source_a']['datasource_id'] == 'a'
    assert payload['source_b']['datasource_id'] == 'b'
    assert all(card['datasource_id']=='a' for card in payload['source_a']['tables'])
    assert all(card['datasource_id']=='b' for card in payload['source_b']['tables'])
    assert all(cards[a]['datasource_id'] != cards[b]['datasource_id'] for a,b in payload['comparison_pairs'])


def test_cross_validator_rejects_same_source_even_when_fields_exist():
    tables, cards = fixture()
    engine, _ = engine_for(cards, {'a':{}, 'b':{}})
    ids = [key for key,card in cards.items() if card['datasource_id']=='a']
    available = {key:{field['name'] for field in card['columns']} for key,card in cards.items()}
    with pytest.raises(DiscoveryIncomplete, match='same_source_relation_rejected'):
        engine.validated_proposal({'source_table_id':ids[0], 'target_table_id':ids[1],
            'key_pairs':[{'source':'customer_id','target':'customer_id'}], 'reason':'同库关系'}, available)


class CrossModel:
    def __init__(self, cards, invalid_pair=None):
        self.cards = cards
        self.invalid_pair = invalid_pair
        self.calls = []

    async def complete_json(self, system, user):
        payload = json.loads(user)
        self.calls.append(payload)
        pair = tuple(payload['comparison_pairs'][0])
        if pair == self.invalid_pair:
            a = next(key for key,card in self.cards.items() if card['datasource_id']=='a')
            return {'relations':[{'source_table_id':pair[0], 'target_table_id':a,
                'key_pairs':[{'source':'customer_id','target':'customer_id'}], 'reason':'同库关系应被拒绝'}]}
        source_field = 'customer_id'
        return {'relations':[{'source_table_id':pair[0], 'target_table_id':pair[1],
            'key_pairs':[{'source':source_field,'target':'id'}], 'reason':'客户编号引用客户主档'}]}


def payloads_for_pair(engine, pair):
    left = wire_card(engine.cards[pair[0]])
    right = wire_card(engine.cards[pair[1]])
    return {'phase':'cross_source_compare',
        'source_a':{'datasource_id':'a','tables':[left]},
        'source_b':{'datasource_id':'b','tables':[right]},
        'comparison_pairs':[list(pair)], 'shared_keyword_evidence':[engine.pair_evidence[pair]]}


def test_failed_batch_does_not_block_other_cross_source_pairs():
    tables, cards = fixture()
    records = {'a':{'客户编号':{'keyword':'客户编号','items':None}},
               'b':{'客户编号':{'keyword':'客户编号','items':None}}}
    model = CrossModel(cards)
    engine, state = engine_for(cards, records, model)
    engine.recall()
    pairs = sorted(engine.pair_evidence)
    model.invalid_pair = pairs[1]
    engine.cross_regions = lambda: iter([payloads_for_pair(engine, pair) for pair in pairs])
    proposals = asyncio.run(engine.run())
    assert len(proposals) == len(pairs) - 1
    assert engine.summary['cross_failed_batches'] == 0
    assert all(proposal['shared_keywords'] == ['客户编号'] for proposal in proposals)
    assert len(model.calls) == len(pairs)
    assert engine.stage_result('cross_discovery') is not None
    assert engine.scope['discarded_relations']



def test_large_keyword_is_batched_instead_of_skipped(monkeypatch):
    _,cards=fixture()
    records={'a':{'客户编号':{'keyword':'客户编号','items':None}},
             'b':{'客户编号':{'keyword':'客户编号','items':None}}}
    monkeypatch.setenv('CROSS_SOURCE_MAX_TABLES_PER_KEYWORD','1')
    engine,_=engine_for(cards,records)
    assert len(engine.recall())==4
    assert engine.summary['cross_oversized_keywords']
    assert sum(len(p['comparison_pairs']) for p in engine.cross_regions())==4


def test_pair_budget_is_explicitly_incomplete_and_never_cached_complete(monkeypatch):
    _,cards=fixture()
    records={'a':{'客户编号':{'keyword':'客户编号','items':None}},
             'b':{'客户编号':{'keyword':'客户编号','items':None}}}
    monkeypatch.setenv('CROSS_SOURCE_MAX_CANDIDATE_PAIRS','1')
    engine,_=engine_for(cards,records,CrossModel(cards))
    asyncio.run(engine.run())
    assert engine.summary['cross_pair_budget_exceeded']
    assert engine.stage_result('cross_discovery') is None


def test_converted_candidate_marks_scope_and_shared_keyword_evidence():
    tables, cards = fixture()
    a_order = next(key for key,card in cards.items() if card['physical_table']=='public.orders')
    b_customer = next(key for key,card in cards.items() if card['physical_table']=='crm.customer')
    proposal = {'source_table_id':a_order,'target_table_id':b_customer,
        'key_pairs':[{'source':'customer_id','target':'id'}], 'reason':'客户编号引用客户主档',
        'shared_keywords':['客户编号']}
    valid, discarded = convert_model_candidates([proposal], cards, tables, 'demo')
    assert not discarded and len(valid)==1
    assert valid[0]['scope']=='cross_source'
    assert valid[0]['source_datasource_id']=='a' and valid[0]['target_datasource_id']=='b'
    assert {'kind':'shared_keywords','keywords':['客户编号']} in valid[0]['evidence']


def test_stored_work_loader_uses_raw_keywords_and_latest_saved_retrieval():
    _, cards = fixture()
    source_cards = {key:card for key,card in cards.items() if card['datasource_id']=='a'}
    table_id = next(iter(source_cards))
    rows = [
        ('retrieval', {'keyword':'客户编号','total':1,'items':[{'table_id':table_id,'matches':['new']}]}, 3),
        ('keywords', {'keywords':['客户编号','单表词']}, 2),
        ('retrieval', {'keyword':'客户编号','total':0,'items':[]}, 1),
    ]
    class Result:
        def fetchall(self): return rows
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,*args): return Result()
    repository = SimpleNamespace(dsn='postgres', _connect=lambda:Connection())
    records = load_stored_keyword_records(repository, 'a', source_cards)
    assert set(records)=={'客户编号','单表词'}
    assert records['单表词']['items'] is None
    assert records['客户编号']['items'][0]['matches']==['new']


def test_cross_job_persists_candidates_preserves_disabled_and_scopes_pending():
    source = {'id':'public.event','name':'event','columns':[
        {'name':'customer_id','type':'bigint','comment':'客户编号'},
        {'name':'account_id','type':'bigint','comment':'账户编号'}]}
    customer = {'id':'crm.customer','name':'customer','columns':[{'name':'id','type':'bigint','comment':'客户编号'}]}
    account = {'id':'crm.account','name':'account','columns':[{'name':'id','type':'bigint','comment':'账户编号'}]}
    left_tables = [source]
    right_tables = [customer, account]
    left_hash = tables_digest(left_tables)
    right_hash = tables_digest(right_tables)
    repository = ModelingRepository(dsn='')
    repository.save_package('a', left_hash, {'objects':[],'relations':[]}, 'ready', 0)
    repository.save_package('b', right_hash, {'objects':[],'relations':[]}, 'ready', 0)
    disabled = _candidate('a','b',source,['customer_id'],customer,['id'],'cross_source',[],status='disabled',domain='demo')
    disabled['reviewed_by'] = 'reviewer'
    repository.replace_candidates([disabled], left_hash, right_hash)
    good = _candidate('a','b',source,['account_id'],account,['id'],'cross_source',[],status='accepted',domain='demo')
    records = {
        'a':SimpleNamespace(id='a', status='healthy', scan_result={'tables':left_tables}),
        'b':SimpleNamespace(id='b', status='healthy', scan_result={'tables':right_tables}),
    }
    service = ModelingService(None, None, repository=repository, ontology_service=None)
    service.datasources = SimpleNamespace(repository=SimpleNamespace(get=lambda key:records[key]))
    async def review(candidates, table_maps, job, **kwargs):
        candidates.extend([dict(disabled), dict(good)])
        job['details']['cross_failed_batches'] = 1
    service._semantic_review = review
    job = repository.create_job('a', kind='cross_source', peer_datasource_id='b', domain='demo')
    asyncio.run(service._run_cross_source(job))
    values = repository.list_cross_candidates('a','b','demo')
    assert job['status'] == 'completed_with_pending'
    assert next(item for item in values if item['id']==disabled['id'])['status'] == 'disabled'
    assert not any(item['id']==good['id'] for item in values)
    assert job['phase']=='incomplete'
    assert job['details']['relationships_updated'] is False
