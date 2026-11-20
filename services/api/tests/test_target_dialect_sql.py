import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from semora_contracts import SQLProgramStep,ExecutionResult
from app.sql_dialect import readonly_tree,compile_step,dialect_for
from app.sql_errors import SQLIssue
from app.datasources import DataSourceRecord
from test_sql_native import sources, native_harness
from helpers import query_model_calls
from app.executor_errors import ExecutorOperationError


def test_mysql_quoted_keyword_and_native_date_function(sources):
    step=SQLProgramStep(name='q',purpose='查看记录',data_source_id='b',dialect='mysql',
        sql='SELECT `group`, DATE_SUB(CURRENT_DATE, INTERVAL 3 DAY) AS cutoff FROM events LIMIT 2',
        output_columns=['group','cutoff'])
    result=compile_step(step,sources)
    assert '`group`' in result.physical_sql and 'DATE_SUB' in result.physical_sql
    assert result.compilation['source_dialect']==result.dialect=='mysql'
    with pytest.raises(SQLIssue):readonly_tree(step.sql,['events'],'postgres')


@pytest.mark.parametrize('kind,dialect,sql',[
    ('mysql','mysql','SELECT `id` FROM records WHERE id=:key LIMIT 2'),
    ('postgresql','postgres','SELECT "id" FROM records WHERE id=:key LIMIT 2'),
    ('sqlserver','tsql','SELECT TOP 2 [id] FROM records WHERE id=:key'),
    ('oracle','oracle','SELECT "id" FROM records WHERE "id"=:key FETCH FIRST 2 ROWS ONLY'),
])
def test_native_dialects_parse_bind_and_render_without_pg_intermediate(sources,kind,dialect,sql):
    from app.datasources import datasource_service
    datasource_service.repository.save(DataSourceRecord('native','native',kind,'NOT_A_CREDENTIAL'))
    query=SQLProgramStep(name='q',purpose='查询记录',data_source_id='native',dialect=dialect,
        sql=sql,parameters={'key':7},output_columns=['id'])
    artifact=compile_step(query,{'objects':[{'table':'records','dataSourceId':'native'}]})
    assert artifact.parameters=={'p0':7}
    assert '%(p0)s' in artifact.physical_sql
    assert artifact.compilation['source_dialect']==dialect
    assert artifact.semantic_sql==sql


def test_detected_compatibility_mode_selects_actual_sql_dialect(sources):
    from app.datasources import datasource_service
    for kind in ('vastbase','oceanbase'):
        datasource_service.repository.save(DataSourceRecord('native','native',kind,'NOT_A_CREDENTIAL',
            scan_result={'connection':{'detected_compatibility_mode':'oracle'}}))
        assert dialect_for('native')=='oracle'


def test_db_execution_failure_passes_sql_and_detailed_error_to_repair(monkeypatch):
    workflow,session,execute,_,model,_=native_harness(monkeypatch)
    original=deepcopy(model.complete_json.return_value)
    execute.side_effect=[ExecutorOperationError('schema',1054,{'message':"Unknown column in field list",'column':'missing','sqlstate':'42S22'}),
        ExecutionResult(columns=['id'],rows=[[1]],row_count=1,duration_ms=1)]
    model.complete_json.side_effect=[original,{'sql':'SELECT id AS id FROM items ORDER BY id','change_summary':'修正列引用'}]
    run=asyncio.run(workflow.start(session.id,'列出对象',domain='test'))
    assert run.status.value=='completed',run.error
    repair=json.loads(query_model_calls(model)[1].args[1])
    assert repair['editable_baseline']['sql']==original['steps'][0]['sql']
    assert repair['editable_baseline']['dialect']=='postgres'
    assert 'LIMIT' in repair['execution_diagnostic']['target_sql']
    assert repair['execution_diagnostic']['error']['diagnostic']['column']=='missing'
    assert repair['execution_diagnostic']['error']['diagnostic']['sqlstate']=='42S22'
    assert run.agent_memory['sql_repair_count']==[{'attempts':1}]


def test_vendor_sql_diagnostic_preserves_reason_and_masks_literal_values():
    import pymysql
    from services.executor.app.errors import database_error
    exc=pymysql.err.ProgrammingError(1064,"You have an error in your SQL syntax; near 'private-value' at line 2")
    diagnostic=database_error(exc,'query').detail['diagnostic']
    assert 'SQL syntax' in diagnostic['message'] and 'line 2' in diagnostic['message']
    assert 'private-value' not in diagnostic['message']


def test_execution_repair_budget_allows_three_calls_and_stops_fourth(monkeypatch):
    workflow,session,execute,_,model,_=native_harness(monkeypatch)
    original=deepcopy(model.complete_json.return_value)
    model.complete_json.side_effect=[original]+[
        {'sql':'SELECT id'+('+0'*n)+' AS id FROM items ORDER BY id','change_summary':f'修正{n}'}
        for n in range(1,4)]
    execute.side_effect=ExecutorOperationError('syntax',1064,{'message':'Invalid SQL syntax at line 1'})
    run=asyncio.run(workflow.start(session.id,'列出对象',domain='test'))
    assert run.status.value=='failed' and run.result is None
    assert len(query_model_calls(model))==4
    assert execute.await_count==4
    assert run.agent_memory['sql_repair_count']==[{'attempts':3}]


def test_named_parameters_preserve_mysql_literals_comments_and_quoted_identifiers():
    from app.sql_dialect import parse_query
    from sqlglot import exp
    tree=parse_query("SELECT ':key' AS txt, `:field` FROM records WHERE id=:key /* :comment */",'mysql')
    assert [p.this for p in tree.find_all(exp.Placeholder)]==['key']
    assert next(tree.find_all(exp.Literal)).this==':key'
    assert ':field' in {c.name for c in tree.find_all(exp.Column)}
