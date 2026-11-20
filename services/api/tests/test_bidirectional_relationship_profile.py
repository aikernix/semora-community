"""Random complete keys, directional inclusion and non-unique direct references."""
import re
import sqlite3
from copy import deepcopy

import pytest
import sqlglot
from services.executor.app import main as executor
from app.relationship_evidence import finalize_relationships
from app.relationship_review_policy import review_reasons
from test_relationship_research import verdict


def test_cross_source_random_composite_keys_guards_and_duplicate_fanout(monkeypatch):
    databases = {sid: sqlite3.connect(':memory:') for sid in ('a', 'b')}
    databases['a'].execute('CREATE TABLE events(scope TEXT, ref INT, active INT)')
    databases['a'].executemany('INSERT INTO events VALUES(?,?,1)', [('x', i) for i in range(1000)])
    databases['a'].execute("INSERT INTO events VALUES('x',2000,0)")
    databases['b'].execute('CREATE TABLE registry(scope TEXT, code INT, kind TEXT)')
    databases['b'].executemany('INSERT INTO registry VALUES(?,?,?)', [('x', i, 'valid') for i in range(20) for _ in range(2)])
    databases['b'].executescript("INSERT INTO registry VALUES('x',2000,'valid');INSERT INTO registry VALUES('x',999,'other');")
    calls = []
    def query(body, token=None):
        sql = re.sub(r'%\(([A-Za-z0-9_]+)\)s', r':\1', body.sql)
        sql = sqlglot.transpile(sql, read='postgres', write='sqlite')[0]
        calls.append((body.datasource.datasource_id, sql, body.parameters))
        cursor = databases[body.datasource.datasource_id].execute(sql, body.parameters)
        return {'rows': [list(r) for r in cursor.fetchall()]}
    monkeypatch.setattr(executor, 'execute_query', query)
    body = executor.ProfileInput(datasource=dict(datasource_id='a',type='postgresql',secret_ref='DATASOURCE_TEST_URL'),
        target_datasource=dict(datasource_id='b',type='postgresql',secret_ref='DATASOURCE_TEST_URL'),
        source_table='events',source_columns=['scope','ref'],target_table='registry',target_columns=['scope','code'],
        sample_rows=100,guards=[dict(side='source',field='active',operator='eq',value=1),
                               dict(side='target',field='kind',operator='eq',value='valid')])
    result = executor.profile_relationship(body)
    assert result['method'] == 'bidirectional_random_membership_v4'
    assert result['forward']['source_nonnull_distinct_keys'] == 100
    assert result['forward']['directional_coverage'] <= .2
    assert result['reverse']['source_nonnull_distinct_keys'] == 21
    assert result['reverse']['matched_keys'] == 20  # inactive source row excluded in reverse too
    assert result['reverse']['source_exhausted']
    assert sum('RANDOM()' in sql for _, sql, _ in calls) == 2
    assert all('LIMIT' in sql for _, sql, _ in calls)
    assert 'signature' not in result['forward'] and 'signature' not in result['reverse']
    assert result['raw_values_retained'] is False
    # Reverse sample includes every target key. Grouping the forward lookup is
    # separately exercised by using a complete source sample in the reverse call.
    reversed_body = body.model_copy(update={'datasource':body.target_datasource,'target_datasource':body.datasource,
        'source_table':'registry','target_table':'events','source_columns':['scope','code'],'target_columns':['scope','ref'],
        'guards':[g.model_copy(update={'side':'target' if g.side=='source' else 'source'}) for g in body.guards]})
    second = executor.profile_relationship(reversed_body)
    assert second['reverse']['max_matches_per_key'] in {0, 2}


@pytest.mark.parametrize('dialect,fragment', [('mysql','RAND()'),('postgres','RANDOM()'),('oracle','DBMS_RANDOM.VALUE'),('tsql','NEWID()')])
def test_random_expression_uses_per_row_database_dialect(monkeypatch, dialect, fragment):
    calls=[]
    monkeypatch.setattr(executor.adapter_registry,'dialect',lambda _:dialect)
    def query(body, token=None):
        calls.append(body.sql)
        return {'rows': []}
    monkeypatch.setattr(executor,'execute_query',query)
    result=executor.profile_relationship(executor.ProfileInput(datasource=dict(datasource_id='s',type='postgresql',secret_ref='DATASOURCE_TEST_URL'),
        source_table='events',source_columns=['ref'],target_table='registry',target_columns=['code']))
    assert len(calls)==2 and all(fragment in sql for sql in calls)
    assert result['forward']['source_exhausted'] and result['reverse']['source_exhausted']


def profile(forward=.01, reverse=1, matches=2):
    def direction(coverage):
        return dict(source_nonnull_distinct_keys=200,matched_keys=matches if coverage<1 else 200,
                    directional_coverage=coverage,source_exhausted=False)
    return dict(method='bidirectional_random_membership_v4',**direction(forward),
                forward=direction(forward),reverse=direction(reverse))


def proposed(stats, kind='entity_reference'):
    decision={**verdict('join'),'relationship_kind':kind}
    return dict(id='join',status='unresolved',evidence_grade='medium',scope='single_source',
                source_columns=['ref'],target_columns=['code'],evidence=[
                    dict(kind='value_profile',statistics=stats),dict(kind='model_semantic_review',**decision),
                    dict(kind='independent_review',**decision),dict(kind='join_contract',valid=True)])


def test_high_reverse_coverage_supports_nonunique_reference():
    c=proposed(profile())
    assert 'data_contradiction' not in review_reasons(c,verdict('join'))
    finalize_relationships([c])
    assert c['status']=='accepted'
    assert 'target_uniqueness_unverified' not in c['evidence'][-1]['reasons']


@pytest.mark.parametrize('initial', ['accepted', 'unresolved'])
@pytest.mark.parametrize('cross_source', [False, True])
def test_profile_timeout_is_review_queue_not_technical_pending(initial,cross_source):
    stats=profile(.1,.1)
    stats['reverse']={'status':'unavailable','db_error_code':3024}
    c=proposed(stats)
    c['status']=initial
    finalize_relationships([c],cross_source=cross_source)
    assert c['status']=='pending' and c['research_pending'] is False
    assert c['evidence'][-1]['reasons']==['profile_timeout']
    assert any(e.get('kind')=='profile_timeout' and '超时' in e['reason'] for e in c['evidence'])


def test_cross_source_100_keys_at_80_percent_can_pass_despite_other_direction_timeout():
    stats=profile(.8,0)
    stats['forward'].update(source_nonnull_distinct_keys=100,matched_keys=80)
    stats['reverse']={'status':'unavailable','db_error_code':3024}
    c=proposed(stats)
    finalize_relationships([c],cross_source=True)
    assert c['status']=='accepted'


def test_low_coverage_without_timeout_does_not_get_timeout_reason():
    c=proposed(profile(.79,.79))
    finalize_relationships([c],cross_source=True)
    assert c['status']!='accepted'
    assert not any(e.get('kind')=='profile_timeout' for e in c['evidence'])


def test_timeout_does_not_block_cross_publication_or_keep_old_timed_out_edge():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.datasource_modeling import ModelingRepository, ModelingService, _candidate, tables_digest
    source={'id':'events','name':'events','columns':[{'name':'ref','type':'bigint'},{'name':'other','type':'bigint'}]}
    target={'id':'registry','name':'registry','columns':[{'name':'code','type':'bigint'}]}
    repository=ModelingRepository(dsn='')
    records={sid:SimpleNamespace(id=sid,scan_result={'tables':[t]}) for sid,t in [('a',source),('b',target)]}
    for sid,t in [('a',source),('b',target)]:
        repository.save_package(sid,tables_digest([t]),{'objects':[],'relations':[]},'ready',0)
    good=_candidate('a','b',source,['ref'],target,['code'],'cross_source',proposed(profile())['evidence'],status='accepted',domain='demo')
    timeout_stats=profile(.1,.1)
    timeout_stats['reverse']={'status':'unavailable','db_error_code':3024}
    timed=_candidate('a','b',source,['other'],target,['code'],'cross_source',proposed(timeout_stats)['evidence'],status='accepted',domain='demo')
    configuration={'objects':[
        {'name':'events','dataSourceId':'a','metadata':{'sourceTableId':'events'},'properties':[{'column':n,'name':n} for n in ['ref','other']]},
        {'name':'registry','dataSourceId':'b','metadata':{'sourceTableId':'registry'},'properties':[{'column':'code','name':'code'}]}],
        'relations':[{'name':'old','from':'events','to':'registry','metadata':{'candidateId':timed['id']}}]}
    ontology=SimpleNamespace(get_configuration=AsyncMock(return_value=configuration),save_configuration=AsyncMock())
    service=ModelingService(SimpleNamespace(repository=SimpleNamespace(get=records.__getitem__)),None,repository=repository,ontology_service=ontology)
    async def review(candidates,*args,**kwargs):candidates.extend([good,timed])
    service._semantic_review=review
    job=repository.create_job('a',kind='cross_source',peer_datasource_id='b',domain='demo')
    asyncio.run(service._run_cross_source(job))
    assert job['status']=='completed' and job['details']['relationships_updated'] is True
    assert timed['status']=='pending'
    published=ontology.save_configuration.await_args.args[1]['relations']
    assert [r['metadata']['candidateId'] for r in published]==[good['id']]
    assert any(c['id']==timed['id'] and c['status']=='pending' for c in repository.list_cross_candidates('a','b','demo'))


def test_api_preserves_double_timeout_as_relation_evidence():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from app.datasource_modeling import ModelingRepository,ModelingService
    source=SimpleNamespace(id='a',type='mysql',secret_ref='DATASOURCE_A_URL',
        scan_result={'tables':[{'id':'events','columns':[{'name':'ref','type':'bigint'}]}]})
    unavailable={'status':'unavailable','db_error_code':3024}
    fetch=AsyncMock(side_effect=HTTPException(503,{'profile_id':'p','directions':{
        'forward':unavailable,'reverse':unavailable}}))
    service=ModelingService(None,fetch,repository=ModelingRepository(dsn=''))
    candidate={'source_table_id':'events','target_table_id':'events','source_columns':['ref'],
        'target_columns':['ref'],'condition_template':{}}
    result=asyncio.run(service._profile(source,candidate))
    assert result['forward']==unavailable and result['reverse']==unavailable
    assert fetch.await_args.kwargs['json']['sample_rows']==100
    c=proposed(result)
    finalize_relationships([c])
    assert c['status']=='pending' and c['research_pending'] is False


def test_positive_partial_coverage_cannot_bypass_threshold_with_model_agreement():
    c=proposed(profile(.01,.03))
    finalize_relationships([c])
    assert c['status']=='unresolved'
    assert 'insufficient_value_coverage' in c['evidence'][-1]['reasons']
    for e in c['evidence']:
        if e['kind']=='independent_review':e['key_domain']='unknown'
    finalize_relationships([c])
    assert c['status']=='unresolved'


@pytest.mark.parametrize('forward,reverse,accepted', [(.799,.799,False),(.80,.01,True),(.01,.80,True),(.01,.025,False),(1,0,True),(0,1,True)])
def test_exact_eighty_percent_boundary_in_either_direction(forward,reverse,accepted):
    c=proposed(profile(forward,reverse))
    c['evidence'].extend([{'kind':'explicit_field_reference'},{'kind':'structural_acceptance'},
                         {'kind':'verified_partial_scope'}])
    for e in c['evidence']:
        if e.get('kind') in {'model_semantic_review','independent_review'}:
            e.update(coverage_expectation='partial',coverage_assessment='incomplete_reference_data')
    finalize_relationships([c])
    assert (c['status']=='accepted') is accepted
    assert c['evidence'][-1]['required_coverage']==.8


@pytest.mark.parametrize('kind', ['shared_entity','unknown','lineage'])
def test_overlapping_values_do_not_approve_unsupported_roles(kind):
    c=proposed(profile(1,1),kind)
    finalize_relationships([c])
    assert c['status']!='accepted'


def test_empty_and_unmatched_samples_do_not_become_negative_or_positive_proof():
    c=proposed(profile(0,0,matches=0))
    finalize_relationships([c])
    assert c['status']=='unresolved'
    c=proposed(profile())
    for e in c['evidence']:
        if e['kind']=='independent_review':e['coverage_assessment']='identifier_conflict'
    finalize_relationships([c])
    assert c['status']=='unresolved'  # one good direction cannot override an actual domain conflict


def test_failed_direction_is_explicit_and_does_not_discard_other_evidence(monkeypatch):
    def query(body,token=None):
        if 'registry' in body.sql and 'ORDER BY' in body.sql:raise TimeoutError()
        return {'rows':[[1]]}
    monkeypatch.setattr(executor,'execute_query',query)
    result=executor.profile_relationship(executor.ProfileInput(datasource=dict(datasource_id='s',type='postgresql',secret_ref='DATASOURCE_TEST_URL'),
        source_table='events',source_columns=['ref'],target_table='registry',target_columns=['code']))
    assert result['forward']['status']=='available' and result['forward']['directional_coverage']==1
    assert result['reverse']['status']=='unavailable'


def test_low_coverage_stops_without_calling_a_second_model_for_approval():
    import asyncio
    from app.relationship_research import research
    c=proposed(profile(.01,.025))
    calls=[]
    class Model:
        async def complete_json(self,system,user):
            calls.append(system)
            return {'decisions':[verdict(c['id'])]}
    async def fetch(*args):raise AssertionError('random data evidence is already present')
    async def persist(values):finalize_relationships(values)
    async def progress(*args):pass
    asyncio.run(research([c],lambda c:{'candidate_id':c['id'],'evidence':c['evidence']},fetch,Model(),persist,progress))
    assert len(calls)==1 and c['status']=='unresolved'
    assert c['review_required'] is False
    assert 'insufficient_value_coverage' in c['evidence'][-1]['reasons']


def test_cached_partial_acceptance_from_previous_policy_is_not_restored():
    from app.relationship_research import restore_checkpoint
    old={'source_schema_hash':'a','target_schema_hash':'b','status':'accepted',
         'evidence':[{'kind':'research_complete','version':'query-relationship-research-v22-bidirectional-random'}]}
    current={}
    assert not restore_checkpoint(current,old,'a','b') and not current
