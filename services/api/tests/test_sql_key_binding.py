import re

import duckdb
import pytest
from sqlglot import exp
from pydantic import ValidationError
from semora_contracts import SQLInputBinding, SQLProgramStep, ExecutionResult
from app import datasources
from app.datasources import DataSourceRecord, DataSourceRepository
from app.sql_dialect import compile_step, parse_query
from app.sql_errors import SQLIssue


@pytest.fixture
def target(monkeypatch):
    monkeypatch.delenv('METADATA_DATABASE_URL', raising=False)
    repo=DataSourceRepository()
    monkeypatch.setattr(datasources.datasource_service,'repository',repo)
    def setup(kind,dialect,version):
        repo.save(DataSourceRecord('target','Target',kind,'NO_CREDENTIAL',scan_result={
            'connection':{'version':version,'detected_compatibility_mode':dialect},
            'capabilities':{'dialect':dialect}}))
        return {'dataSourceId':'target','objects':[{'name':'item','table':'items','dataSourceId':'target',
            'primaryKey':'id','properties':[{'name':c,'type':'integer'} for c in ('id','a','b','enabled')]}]}
    return setup


def run_bound(artifact):
    sql=re.sub(r'%\(([A-Za-z_][A-Za-z0-9_]*)\)s',r':\1',artifact.physical_sql)
    tree=parse_query(sql,artifact.dialect)
    if artifact.dialect=='oracle':
        # DuckDB has no Oracle ROWNUM pseudocolumn. Emulate only the compiler's
        # exact outer safety cap; the predicate under test remains untouched.
        guard=tree.args['where'].this
        assert isinstance(guard,exp.LTE) and guard.this.name=='ROWNUM'
        assert guard.expression.name=='1001'
        source=tree.find(exp.From).this
        assert isinstance(source,exp.Subquery)
        tree=source.this.copy().limit(1001)
    sql=tree.sql(dialect='duckdb')
    with duckdb.connect(':memory:') as c:
        c.execute('CREATE TABLE items(id INT,a INT,b INT,enabled INT)')
        c.executemany('INSERT INTO items VALUES(?,?,?,?)',[
            [1,1,2,0],[2,2,1,1],[3,1,None,1],[4,2,None,0],
            [5,None,2,1],[6,None,None,1],[7,1,1,1],[8,2,2,1]])
        return c.execute(sql,artifact.parameters).fetchall()


TARGETS=[('mysql','mysql','5.7.28'),('postgresql','postgres','12.1'),
         ('sqlserver','tsql','15.0'),('sqlserver','tsql','16.0'),
         ('oracle','oracle','11.2'),('oracle','oracle','19.3'),
         ('vastbase','postgres','2.2'),('vastbase','oracle','2.2'),
         ('oceanbase','mysql','3.2'),('oceanbase','oracle','4.2'),('goldendb','mysql','6.0')]


@pytest.mark.parametrize('kind,dialect,version',TARGETS)
@pytest.mark.parametrize('legacy',[False,True])
@pytest.mark.parametrize('null_safe',[[],['b'],['a','b']])
def test_complete_tuples_null_policy_and_other_filters_survive_target_lowering(target,kind,dialect,version,legacy,null_safe):
    ontology=target(kind,dialect,version)
    expression='(a,b) IN (:keys)' if legacy else 'SEMORA_KEYS_MATCH(:keys,a,b)'
    step=SQLProgramStep(name='read',purpose='Read matching records',engine='source',dialect=dialect,
        data_source_id='target',sql=f'SELECT id FROM items WHERE {expression} AND enabled=:enabled ORDER BY id',
        output_columns=['id'],parameters={'enabled':1},inputs=[SQLInputBinding(
            source_step='roots',source_columns=['a','b'],parameter='keys',null_safe_columns=null_safe)])
    roots=ExecutionResult(columns=['a','b'],rows=[[1,2],[2,1],[1,None],[2,None],[None,2],[None,None],[1,2]],row_count=7,duration_ms=0)
    artifact=compile_step(step,ontology,{'roots':roots})
    assert 'SEMORA_KEYS_MATCH' not in artifact.physical_sql
    assert '(a, b) IN' not in artifact.physical_sql
    expected=[(2,)] if not null_safe else [(2,),(3,)] if null_safe==['b'] else [(2,),(3,),(5,),(6,)]
    assert run_bound(artifact)==expected


@pytest.mark.parametrize('null_safe',[[],['a']])
def test_scalar_binding_only_matches_null_when_explicitly_requested(target,null_safe):
    ontology=target('sqlserver','tsql','15.0')
    step=SQLProgramStep(name='read',purpose='Read records',dialect='tsql',data_source_id='target',
        sql='SELECT id FROM items WHERE SEMORA_KEYS_MATCH(:keys,a) ORDER BY id',output_columns=['id'],
        inputs=[SQLInputBinding(source_step='roots',source_columns=['a'],parameter='keys',null_safe_columns=null_safe)])
    roots=ExecutionResult(columns=['a'],rows=[[None]],row_count=1,duration_ms=0)
    artifact=compile_step(step,ontology,{'roots':roots})
    assert run_bound(artifact)==([(5,),(6,)] if null_safe else [])


@pytest.mark.parametrize('kind,dialect,version',TARGETS)
@pytest.mark.parametrize('predicate',['NOT (a IS NOT DISTINCT FROM b)','a IS DISTINCT FROM b'])
def test_null_safe_comparison_is_two_valued_even_under_not(target,kind,dialect,version,predicate):
    ontology=target(kind,dialect,version)
    step=SQLProgramStep(name='read',purpose='Find different values',dialect=dialect,data_source_id='target',
        sql=f'SELECT id FROM items WHERE {predicate} ORDER BY id',output_columns=['id'])
    artifact=compile_step(step,ontology)
    assert run_bound(artifact)==[(1,),(2,),(3,),(4,),(5,)]
    if dialect in {'tsql','oracle'}:assert 'DISTINCT FROM' not in artifact.physical_sql


def test_null_policy_cannot_name_an_unbound_column():
    with pytest.raises(ValidationError,match='null_safe_columns'):
        SQLInputBinding(source_step='roots',source_columns=['a'],parameter='keys',null_safe_columns=['b'])


@pytest.mark.parametrize('expression',['SEMORA_KEYS_MATCH(:keys,a)', 'SEMORA_KEYS_MATCH(:keys,a,b+1)'])
def test_incomplete_or_expression_key_mapping_is_rejected(target,expression):
    ontology=target('mysql','mysql','5.7')
    step=SQLProgramStep(name='read',purpose='Read',dialect='mysql',data_source_id='target',
        sql=f'SELECT id FROM items WHERE {expression}',output_columns=['id'],
        inputs=[SQLInputBinding(source_step='roots',source_columns=['a','b'],parameter='keys')])
    roots=ExecutionResult(columns=['a','b'],rows=[[1,2]],row_count=1,duration_ms=0)
    with pytest.raises(SQLIssue,match='键集目标列'):compile_step(step,ontology,{'roots':roots})


@pytest.mark.parametrize('version,strategy,expected',[('11.2','rownum','ROWNUM'),('19.3','sqlglot','FETCH FIRST')])
def test_pagination_honors_actual_source_strategy_instead_of_prompt_literal(target,version,strategy,expected):
    ontology=target('oracle','oracle',version)
    datasources.datasource_service.repository.get('target').scan_result['capabilities']['pagination_strategy']=strategy
    step=SQLProgramStep(name='read',purpose='Read records',dialect='oracle',data_source_id='target',
        sql='SELECT id FROM items ORDER BY id',output_columns=['id'])
    artifact=compile_step(step,ontology)
    assert expected in artifact.physical_sql and 'LIMIT' not in artifact.physical_sql
