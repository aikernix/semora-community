"""Generic SQL-native contracts, real relational execution, and bounded repair."""
from helpers import query_model_calls
import asyncio
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from semora_contracts import SQLProgram, SQLProgramStep, SQLInputBinding, ExecutionResult, RunStatus
from app import datasources, sql_authoring, prepared_context
from app.datasources import DataSourceRecord, DataSourceRepository
from app.executor_errors import ExecutorOperationError
from app.sql_dialect import compile_step
from app.result_engine import local_query
from app.sql_errors import SQLIssue


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.delenv('METADATA_DATABASE_URL', raising=False)
    repo=DataSourceRepository()
    for name,kind in [('a','postgresql'),('b','mysql')]:
        repo.save(DataSourceRecord(name,name,kind,'NOT_A_CREDENTIAL',scan_result={'capabilities':{'cte':{'status':'verified'},'window_functions':{'status':'verified'}}}))
    monkeypatch.setattr(datasources.datasource_service,'repository',repo)
    return {'dataSourceId':'a','objects':[
        {'name':'item','table':'items','primaryKey':'id','properties':[{'name':'id','type':'integer'}]},
        {'name':'event','table':'events','dataSourceId':'b','properties':[{'name':'item_id','type':'integer'}]}]}


def step(name='q',sql='SELECT id FROM items',source='a',**kwargs):
    return SQLProgramStep(name=name,purpose='List identifiers',data_source_id=source,sql=sql,
                          output_columns=['id'],**kwargs)


def result(columns,rows,types=None,**kwargs):
    return ExecutionResult(columns=columns,rows=rows,column_types=types or ['BIGINT']*len(columns),
                           row_count=len(rows),duration_ms=0,**kwargs)


def test_native_sql_not_reconstructed_and_scalar_parameters_are_bound(sources):
    value="x' OR 1=1 --"
    artifact=compile_step(step(sql='WITH x AS (SELECT id FROM items WHERE id=:v) SELECT id FROM x',parameters={'v':value}),sources)
    assert 'WITH x AS' in artifact.physical_sql
    assert value not in artifact.physical_sql and value in artifact.parameters.values()
    assert 'LIMIT 1001' in artifact.physical_sql


def test_composite_bindings_preserve_tuples_nulls_and_empty_set(sources):
    binding=SQLInputBinding(source_step='q0',source_columns=['a','b'],parameter='keys')
    query=step(sql='SELECT item_id AS id FROM events WHERE (item_id,kind) IN (:keys)',source='b',inputs=[binding])
    data={'q0':result(['a','b'],[[1,2],[1,2],[2,1],[None,3]])}
    artifact=compile_step(query,sources,data)
    assert list(artifact.parameters.values())==[1,2,2,1]
    assert ' AND ' in artifact.physical_sql and ' OR ' in artifact.physical_sql
    empty=compile_step(query,sources,{'q0':result(['a','b'],[])})
    assert 'FALSE' in empty.physical_sql and empty.parameters=={}
    data['q0'].truncated=True
    with pytest.raises(SQLIssue,match='不完整'):compile_step(query,sources,data)


@pytest.mark.parametrize('sql',[
    'SELECT id FROM items; DELETE FROM items',
    'WITH x AS (DELETE FROM items RETURNING id) SELECT id FROM x',
    'SELECT id INTO another FROM items',
    'SELECT id FROM secret_items',
    'SELECT id FROM items FOR UPDATE',
])
def test_execution_constraints_without_business_rule_whitelists(sources,sql):
    with pytest.raises(SQLIssue):compile_step(step(sql=sql),sources)


def test_untranslatable_features_are_explicit(sources):
    with pytest.raises(SQLIssue,match='转换失败'):
        compile_step(step(sql='SELECT id FROM items TABLESAMPLE BERNOULLI (10)',source='b'),
                     {'dataSourceId':'b','objects':[{'table':'items'}]})


def test_program_dependencies_rejected_in_structured_schema():
    with pytest.raises(ValidationError,match='先前步骤'):
        SQLProgram(summary='x',steps=[step(inputs=[SQLInputBinding(source_step='future',source_columns=['id'],parameter='keys')])])


def test_local_sql_handles_aggregation_outer_join_and_exact_decimal(sources):
    query=step(name='final',source='',engine='local',sql='''
      WITH sums AS (SELECT id,SUM(amount) AS amount FROM detail GROUP BY id)
      SELECT root.id,COALESCE(sums.amount,0) AS amount
      FROM root LEFT JOIN sums ON root.id=sums.id ORDER BY root.rank''')
    tables={'root':result(['id','rank'],[[2,0],[1,1]]),
            'detail':result(['id','amount'],[[1,'9007199254740993.01'],[1,'0.02']],['BIGINT','DECIMAL(38,2)']),
            'unused':result(['id'],[[9]],truncated=True)}
    artifact=compile_step(query,sources,tables)
    actual=local_query(artifact,tables,{'max_result_rows':100,'query_timeout_seconds':5})
    assert actual.rows==[[2,'0.00'],[1,'9007199254740993.03']]
    assert actual.column_types==['BIGINT','DECIMAL(38,2)']
    tables['detail']=result(['id','amount'],[],['BIGINT','DECIMAL(38,2)'])
    assert local_query(artifact,tables,{}).rows==[[2,'0.00'],[1,'0.00']]


def test_local_engine_cannot_read_external_files(sources):
    with pytest.raises(SQLIssue):
        compile_step(step(engine='local',source='',sql="SELECT * FROM read_csv('/private')"),sources,{})


def test_connected_context_does_not_load_an_unneeded_alternate_bridge(sources):
    from app.prepared_context import context_packet
    from unittest.mock import Mock
    ont=deepcopy(sources)
    ont['objects'].append({'name':'bridge','table':'bridge','properties':[{'name':'id','type':'integer'}]})
    ont['relations']=[{'name':'direct','from':'item','to':'event','condition':'item.id=event.item_id'},
        {'name':'alternate1','from':'item','to':'bridge','condition':'item.id=bridge.id'},
        {'name':'alternate2','from':'bridge','to':'event','condition':'bridge.id=event.item_id'}]
    connector=Mock(side_effect=AssertionError('unnecessary bridge expansion'))
    packet=context_packet(SimpleNamespace(analyst=SimpleNamespace(_connect_objects=connector)),
                          SimpleNamespace(),ont,['item','event'])
    assert packet['selected_objects']==['event','item']
    connector.assert_not_called()


def native_harness(monkeypatch,*,questions=None,program=None):
    from helpers import harness
    w,session,execute,estimate=harness(monkeypatch)
    program=program or {'summary':'List requested identifiers','steps':[{
        'name':'q','purpose':'List items','data_source_id':'source','sql':'SELECT id FROM items ORDER BY id',
        'output_columns':['id']}]}
    model=SimpleNamespace(configured=True,complete_messages=AsyncMock(),
                          complete_json=AsyncMock(return_value=deepcopy(program)))
    w.model=model
    async def prepare(workflow,run,question,ontology,scope):
        objects=[{'object':{**deepcopy(o),'dataSourceId':o.get('dataSourceId') or ontology['dataSourceId']}}
                 for o in ontology['objects']]
        value={'packet':{'objects':objects,
                         'selected_objects':[o['name'] for o in ontology['objects']],'knowledge':[],'relations':[]}}
        run.agent_memory['prepared_context']=[value]
        return value
    monkeypatch.setattr(prepared_context,'prepare_context',prepare)
    report={'summary':'List requested identifiers','skeleton':['List item identifiers'],
            'questions':questions or [],'technical_gaps':[]}
    runner=AsyncMock(return_value=(report,[]))
    monkeypatch.setattr(sql_authoring,'run_agent',runner)
    return w,session,execute,estimate,model,runner


def test_full_graph_uses_sql_authoring_not_intent_compiler(monkeypatch):
    w,session,execute,estimate,model,runner=native_harness(monkeypatch)
    run=asyncio.run(w.start(session.id,'List items',domain='test'))
    assert run.status==RunStatus.COMPLETED,run.error
    assert run.result.rows==[[1],[2]]
    assert not hasattr(w, '_compile')
    assert run.plan.sql_program
    assert run.requirement.analysis_steps and run.sql.dialect=='postgres'
    assert 'SQLProgram' in query_model_calls(model)[-1].args[1]
    assert run.gate is run.review is run.approval is None
    from app.control_state import restore_run,run_payload
    restored=restore_run(run_payload(run))
    assert restored.result.rows==[[1],[2]]
    assert restored.execution_receipts['q'].output_hash
    assert restored.sql.requirement_version==restored.requirement.version


def test_analysis_returns_candidate_gap_to_model_for_correction(monkeypatch):
    w, session, execute, _, _, runner = native_harness(monkeypatch)
    report = deepcopy(runner.return_value[0])
    report['required_object_uses'] = [
        {'object': 'item', 'requirement': 'List requested identifiers',
         'reason': 'The requested identifiers are mapped on this object'}
    ]
    invalid = deepcopy(report)
    invalid['technical_gaps'] = [{
        'kind': 'relation', 'summary': 'Candidate relation is unavailable',
        'required_for': 'join', 'objects': ['item', 'candidate'],
        'evidence_refs': ['ontology'], 'resolution': 'Add a relation',
    }]

    async def corrected_submission(*args, **kwargs):
        validate = args[6]
        with pytest.raises(ValueError, match='未证明必要性'):
            validate(invalid)
        validate(report)
        return report, []

    runner.side_effect = corrected_submission
    run = asyncio.run(w.start(session.id, 'List items', domain='test'))
    assert run.status == RunStatus.COMPLETED, run.error
    assert not run.query_rehearsal.technical_gaps
    assert run.query_rehearsal.required_object_uses[0].object == 'item'
    assert execute.await_count == 1


def test_first_preview_display_event_recovers_from_actual_persisted_result(monkeypatch):
    from app.control_state import restore_run,run_payload
    w,session,_,_,_,_=native_harness(monkeypatch)
    run=asyncio.run(w.start(session.id,'List items',domain='test'))
    payload=run_payload(run)
    payload['execution_receipts']={}
    for event in payload['events']:
        if event['event']=='source.step.completed':event['data'].pop('output_hash',None)
    restored=restore_run(payload)
    assert restored.execution_receipts['q'].row_count==2
    assert restored.result.rows==[[1],[2]]


def test_real_agent_runtime_accepts_small_understanding_contract(monkeypatch):
    from semora_agent.runtime import run_agent
    from helpers import NativeModel
    w,session,_,_,model,_=native_harness(monkeypatch)
    native=NativeModel([('AgentSubmission',{'payload':{'summary':'List items','skeleton':['List identifiers']}})])
    native.complete_json=model.complete_json
    w.model=native
    monkeypatch.setattr(sql_authoring,'run_agent',run_agent)
    run=asyncio.run(w.start(session.id,'List items',domain='test'))
    assert run.status==RunStatus.COMPLETED,run.error
    assert len(native.messages)==1 and run.result.rows==[[1],[2]]


def test_explain_error_repaired_with_concrete_diagnostic(monkeypatch):
    w,session,execute,estimate,model,_=native_harness(monkeypatch)
    estimate.side_effect=[ExecutorOperationError('schema','42703',{'column':'bad'}),{'estimated_rows':1}]
    model.complete_json.side_effect=[{'summary':'List items','steps':[{
        'name':'q','purpose':'List items','data_source_id':'source','sql':'SELECT bad AS id FROM items',
        'output_columns':['id']}]},
        {'sql':'SELECT id FROM items ORDER BY id','parameters':{},'change_summary':'Correct column reference'}]
    run=asyncio.run(w.start(session.id,'List items',domain='test'))
    assert run.status==RunStatus.COMPLETED,run.error
    assert run.agent_memory['sql_repair_count']==[{'attempts':1}]
    from semora_contracts import QueryRun
    restored=QueryRun.model_validate({**run.model_dump(mode='json'),'agent_memory':run.agent_memory})
    assert restored.agent_memory['sql_repair_count']==[{'attempts':1}]
    assert '42703' in query_model_calls(model)[-1].args[1] and 'bad' in query_model_calls(model)[-1].args[1]
    assert run.result.rows==[[1],[2]]
    assert estimate.await_count==2 and execute.await_count==1


def test_invalid_local_scope_correction_uses_the_shared_sql_repair_budget(monkeypatch):
    w,session,execute,estimate,model,_=native_harness(monkeypatch)
    correct=deepcopy(model.complete_json.return_value)
    invalid=deepcopy(correct)
    invalid['steps'][0].update(engine='local',data_source_id='')
    model.complete_json.side_effect=[invalid,correct]
    run=asyncio.run(w.start(session.id,'List items',domain='test'))
    assert run.status==RunStatus.COMPLETED,run.error
    assert len(query_model_calls(model))==2 and run.agent_memory['sql_repair_count']==[{'attempts':1}]
    assert 'local不能访问物理表' in query_model_calls(model)[-1].args[1]
    assert estimate.await_count==1 and execute.await_count==1


def test_connection_error_does_not_consume_model_repair(monkeypatch):
    w,session,execute,estimate,model,_=native_harness(monkeypatch)
    estimate.side_effect=ExecutorOperationError('connect',2003)
    run=asyncio.run(w.start(session.id,'List items',domain='test'))
    assert run.status==RunStatus.FAILED and run.sql
    assert len(query_model_calls(model))==1 and not run.agent_memory.get('sql_repair_count')
    execute.assert_not_called()


def test_execution_error_gets_repair_and_revalidation(monkeypatch):
    w,session,execute,estimate,model,_=native_harness(monkeypatch)
    query=execute.side_effect
    calls=0
    async def failure_once(*args):
        nonlocal calls
        calls+=1
        if calls==1:raise ExecutorOperationError('schema','42883',{'message':'function not found'})
        return await query(*args)
    execute.side_effect=failure_once
    model.complete_json.side_effect=[model.complete_json.return_value,
        {'sql':'SELECT id AS id FROM items ORDER BY id','parameters':{},'change_summary':'Correct expression'}]
    run=asyncio.run(w.start(session.id,'List items',domain='test'))
    assert run.status==RunStatus.COMPLETED,run.error
    assert calls==2 and estimate.await_count==2


def test_intermediate_overflow_uses_concrete_feedback_for_safe_source_repair(monkeypatch):
    program={'summary':'Return the latest identifier','steps':[
        {'name':'roots','purpose':'Read identifiers','data_source_id':'source',
         'sql':'SELECT id FROM items','output_columns':['id']},
        {'name':'final','engine':'local','purpose':'Return latest identifier',
         'sql':'SELECT id FROM roots ORDER BY id DESC LIMIT 1','output_columns':['id']}]}
    w,session,execute,estimate,model,_=native_harness(monkeypatch,program=program)
    actual=execute.side_effect
    calls=0
    async def overflow_once(*args):
        nonlocal calls
        calls+=1
        if calls==1:return result(['id'],[[1],[2]],truncated=True)
        value=await actual(*args)
        value.column_types=['BIGINT']
        return value
    execute.side_effect=overflow_once
    model.complete_json.side_effect=[program,{'edits':[]}, {'sql':'SELECT id FROM items ORDER BY id DESC LIMIT 1',
        'change_summary':'Apply the requested Top N at the source; later steps do not change membership'}]
    run=asyncio.run(w.start(session.id,'Return the latest identifier only',domain='test'))
    assert run.status==RunStatus.COMPLETED,run.error
    assert run.result.rows==[[2]] and run.source_results['roots'].rows==[[2]]
    assert run.agent_memory['sql_repair_count']==[{'attempts':1}]


def test_repair_budget_shared_and_result_not_fabricated(monkeypatch):
    w,session,execute,estimate,model,_=native_harness(monkeypatch)
    estimate.side_effect=ExecutorOperationError('syntax',1064)
    model.complete_json.side_effect=[model.complete_json.return_value,
        {'sql':'SELECT id+0 AS id FROM items','change_summary':'First correction'},
        {'sql':'SELECT id*1 AS id FROM items','change_summary':'Second correction'},
        {'sql':'SELECT id+0+0 AS id FROM items','change_summary':'Third correction'},
        {'sql':'SELECT id*1*1 AS id FROM items','change_summary':'Fourth correction'},
        {'sql':'SELECT id+0+0+0 AS id FROM items','change_summary':'Fifth correction'}]
    run=asyncio.run(w.start(session.id,'List items',domain='test'))
    assert run.status==RunStatus.FAILED and run.result is None
    assert len(query_model_calls(model))==4 and estimate.await_count==4
    assert run.agent_memory['sql_repair_count']==[{'attempts':3}]
    execute.assert_not_called()


def test_one_business_confirmation_then_author_without_reanalysis(monkeypatch):
    questions=[{'id':'range','field':'range','question':'Which scope?','reason':'Scope changes results',
        'options':[{'id':'all','value':'all','label':'All items','description':'Include all items'}]}]
    w,session,_,_,model,runner=native_harness(monkeypatch,questions=questions)
    async def scenario():
        run=await w.start(session.id,'List items',domain='test')
        assert run.status==RunStatus.CLARIFYING
        await w.align(run,'test',{'range':'all'})
        return run
    run=asyncio.run(scenario())
    assert run.status==RunStatus.COMPLETED,run.error
    assert runner.await_count==1 and len(query_model_calls(model))==1
    assert 'Include all items' in query_model_calls(model)[-1].args[1]


def test_prompts_are_industry_neutral_and_describe_only_role_contract():
    from semora_agent.sql_prompts import UNDERSTAND, AUTHOR, REPAIR, COVERAGE
    for prompt in (UNDERSTAND,AUTHOR,REPAIR,COVERAGE):
        assert not any(word in prompt.lower() for word in ['银行','教育','贷款','学员','课程','acct_','talk51','bank_'])
        assert len(prompt)<1400
    assert all('JSON' in prompt for prompt in (AUTHOR,REPAIR,COVERAGE))


def test_executor_curated_diagnostic_and_decimal_metadata():
    from services.executor.app.errors import database_error
    from services.executor.app.result_types import column_types,encode_scalar
    error=database_error(Exception(1054,"Unknown column 'x.missing' in 'field list'"),'estimate')
    assert 'x.missing' in error.detail['diagnostic']['message']
    assert column_types([('amount',246,None,None,20,2,None)],True)==['DECIMAL(20,2)']
    assert encode_scalar(Decimal('9007199254740993.01'))=='9007199254740993.01'
    assert column_types([('amount',246,None,None,65,2,None)],True,[(Decimal('9007199254740993.01'),)])==['DECIMAL(18,2)']
    assert column_types([('total',1700,None,None,None,None,None)],False,[(None,)])==['DECIMAL(38,18)']


def test_initial_preview_counter_is_migrated_on_reload():
    from semora_contracts import QueryRun
    run=QueryRun(session_id='s',domain='test',question='List items',agent_memory={'sql_repair_count':[2]})
    assert run.agent_memory['sql_repair_count']==[{'attempts':2}]


@pytest.mark.parametrize('origin,code,expected',[
    ('database',3024,'查询失败，SQL执行超时'),
    ('system',None,'查询失败，单条SQL执行超过30秒'),
])
def test_execution_timeout_retries_once_without_model_repair_and_preserves_failed_sql(monkeypatch,origin,code,expected):
    from app.sql_errors import repairable
    error=ExecutorOperationError('timeout',code,{'timeout_origin':origin})
    assert not repairable(error)
    w,session,execute,_,model,_=native_harness(monkeypatch)
    execute.side_effect=error
    run=asyncio.run(w.start(session.id,'List items',domain='test'))
    assert run.status==RunStatus.FAILED and run.error==expected
    assert execute.await_count==2 and len(query_model_calls(model))==1
    assert not run.agent_memory.get('sql_repair_count') and run.result is None
    failure=run.agent_memory['execution_failure'][-1]
    assert failure['step']=='q' and failure['step_index']==1
    assert failure['timeout_origin']==origin and failure['timeout_seconds']==30
    assert 'FROM items' in failure['sql']
    assert any(e.event=='source.step.failed' and e.data['sql']==failure['sql'] for e in run.events)
    assert sum(e.event=='source.step.retry' for e in run.events)==1
    assert failure['retryable'] is False and failure['retries_remaining']==0
    retry=next(e.data for e in run.events if e.event=='source.step.retry')
    assert retry['retryable'] is True and retry['retries_remaining']==1


@pytest.mark.parametrize('origin,code',[('database',3024),('system',None)])
def test_timeout_retry_succeeds_with_identical_sql_and_no_extra_model_call(monkeypatch,origin,code):
    w,session,execute,_,model,_=native_harness(monkeypatch)
    success=execute.side_effect
    attempts=[]
    async def transient_timeout(*args,**kwargs):
        attempts.append(args)
        if len(attempts)==1:raise ExecutorOperationError('timeout',code,{'timeout_origin':origin})
        return await success(*args,**kwargs)
    execute.side_effect=transient_timeout
    run=asyncio.run(w.start(session.id,'List items',domain='test'))
    assert run.status==RunStatus.COMPLETED and run.result.rows==[[1],[2]]
    assert attempts[0]==attempts[1] and len(attempts)==2
    assert len(query_model_calls(model))==1 and not run.agent_memory.get('sql_repair_count')
    assert not run.agent_memory.get('execution_failure')


@pytest.mark.parametrize('events',[[[1]],[]])
@pytest.mark.parametrize('legacy',[False,True])
def test_cross_source_full_graph_transfers_actual_keys_and_joins_with_sql(monkeypatch,events,legacy):
    import re
    import sqlite3
    import sqlglot
    program={'summary':'Preserve all selected entities and count matching events','steps':[
        {'name':'roots','purpose':'Select entities','data_source_id':'source',
         'sql':'SELECT id FROM items ORDER BY id DESC','output_columns':['id']},
        {'name':'matches','purpose':'Find matching events','data_source_id':'other',
         'sql':'SELECT item_id FROM events WHERE '+('item_id IN (:keys)' if legacy else 'SEMORA_KEYS_MATCH(:keys,item_id)'),
         'inputs':[{'source_step':'roots','source_columns':['id'],'parameter':'keys'}],
         'output_columns':['item_id']},
        {'name':'final','engine':'local','purpose':'Return counts including unmatched entities',
         'sql':'SELECT roots.id,COUNT(matches.item_id) AS n FROM roots LEFT JOIN matches ON roots.id=matches.item_id GROUP BY roots.id ORDER BY roots.id DESC',
         'output_columns':['id','n']}]}
    w,session,_,_,_,_=native_harness(monkeypatch,program=program)
    repository=datasources.datasource_service.repository
    repository.save(DataSourceRecord('other','Second source','mysql','NO_CREDENTIAL'))
    old_loader=w.snapshot_loader
    async def snapshot(domain):
        ref,ont=await old_loader(domain)
        ont['objects'].append({'name':'event','table':'events','dataSourceId':'other',
                               'properties':[{'name':'item_id','type':'integer'}]})
        return ref,ont
    w.snapshot_loader=snapshot
    databases={name:sqlite3.connect(':memory:') for name in ['source','other']}
    databases['source'].executescript('CREATE TABLE items(id INT);INSERT INTO items VALUES(1),(2);')
    databases['other'].execute('CREATE TABLE events(item_id INT)')
    databases['other'].executemany('INSERT INTO events VALUES(?)',events)
    async def execute(sql,params,_,settings):
        if settings['data_source_id']=='other':assert sorted(params.values())==[1,2]
        sql=re.sub(r'%\(([A-Za-z_][A-Za-z0-9_]*)\)s',r':\1',sql)
        sql=sqlglot.transpile(sql,read='mysql' if settings['data_source_id']=='other' else 'postgres',write='sqlite')[0]
        cursor=databases[settings['data_source_id']].execute(sql,params)
        return result([c[0] for c in cursor.description],[list(r) for r in cursor.fetchall()])
    w.execute=AsyncMock(side_effect=execute)
    run=asyncio.run(w.start(session.id,'Show every entity and its event count',domain='test'))
    assert run.status==RunStatus.COMPLETED,run.error
    assert run.result.rows==[[2,0],[1,len(events)]]
    assert w.execute.await_count==2
    assert run.sql.dialect=='duckdb'


def test_http_background_request_reaches_real_result_and_persists(monkeypatch):
    import httpx
    from app import main
    w,session,execute,estimate,_,_=native_harness(monkeypatch)
    monkeypatch.setattr(main,'store',w.store)
    monkeypatch.setattr(main,'workflow',w)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
            response=await client.post(f'/api/v1/sessions/{session.id}/messages',json={'question':'List items','mode':'new'})
            assert response.status_code==202,response.text
            run=w.store.runs[response.json()['id']]
            task=w._tasks.get(run.id)
            if task:await task
            actual=await client.get(f'/api/v1/runs/{run.id}')
            assert actual.status_code==200,actual.text
            assert actual.json()['status']=='completed',actual.text
            assert actual.json()['result']['rows']==[[1],[2]]
    asyncio.run(scenario())
    execute.assert_awaited_once()
