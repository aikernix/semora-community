import json
import logging
import random
import re
import sqlite3

import pytest
import sqlglot

from app.datasource_modeling import sampling_key
from services.executor.app import main as executor


def test_only_unconditional_single_integer_unique_keys_drive_seeks():
    def table(kind, keys, indexes=()):
        return {'columns':[{'name':'k','type':kind}], 'primary_keys':keys,'indexes':list(indexes)}
    assert sampling_key(table('bigint unsigned',[{'columns':['k']}])) == 'k'
    assert sampling_key(table('varchar(64)',[{'columns':['k']}])) is None
    assert sampling_key(table('int',[{'columns':['k','other']}])) is None
    assert sampling_key(table('int',[],[{'columns':['k'],'unique':False}])) is None
    assert sampling_key({'columns':[{'name':'k','type':'int'}],
        'unique_constraints':[{'columns':['k'],'predicate':'active = 1'}]}) is None


def test_index_windows_preserve_complete_keys_guards_and_never_claim_exhaustion(monkeypatch, caplog):
    db = sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE events(pk INTEGER PRIMARY KEY, scope TEXT, ref INTEGER, active INT);'
                     'CREATE TABLE registry(pk INTEGER PRIMARY KEY, scope TEXT, code INTEGER, kind INT);')
    db.executemany('INSERT INTO events VALUES(?,?,?,?)',[(i,'x',i,1) for i in range(1,501)])
    db.executemany('INSERT INTO registry VALUES(?,?,?,?)',[(i,'x',i,2) for i in range(1,501)])
    db.execute("INSERT INTO events VALUES(501,'x',999999,0)")
    db.execute("INSERT INTO registry VALUES(501,'x',888888,3)")
    calls=[]
    rng=random.Random(7)
    monkeypatch.setattr(random,'SystemRandom',lambda:rng)
    def query(body, token=None):
        calls.append(body)
        sql=re.sub(r'%\(([A-Za-z0-9_]+)\)s',r':\1',body.sql)
        sql=sqlglot.transpile(sql,read='postgres',write='sqlite')[0]
        return {'rows':[list(row) for row in db.execute(sql,body.parameters).fetchall()]}
    monkeypatch.setattr(executor,'execute_query',query)
    source={'datasource_id':'a','type':'postgresql','secret_ref':'DATASOURCE_A_URL'}
    body=executor.ProfileInput(datasource=source,source_table='events',target_table='registry',
        source_columns=['scope','ref'],target_columns=['scope','code'],
        source_sampling_key='pk',target_sampling_key='pk',sample_rows=30,
        guards=[{'side':'target','field':'kind','value':2},{'side':'source','field':'active','value':1}])
    with caplog.at_level(logging.INFO,logger='uvicorn.error'):
        result=executor.profile_relationship(body)
    for direction in ['forward','reverse']:
        assert result[direction]['directional_coverage']==1
        assert result[direction]['source_nonnull_distinct_keys']==30
        assert result[direction]['sampling_method']=='approximate_random_index_windows'
        assert result[direction]['uniform_distinct_key_sample'] is False
        assert result[direction]['source_exhausted'] is False
    assert all(c.timeout_ms==30000 for c in calls)
    assert not any('RANDOM()' in c.sql for c in calls)
    assert any('UNION ALL' in c.sql and 'LIMIT 32' in c.sql for c in calls)
    stages=[json.loads(r.message) for r in caplog.records if r.message.startswith('{')]
    assert {'sampling_bounds','sampling_index_windows','matching_keys'} <= {s['stage'] for s in stages}
    assert {s['direction'] for s in stages}=={'forward','reverse'}
    assert len({s['profile_id'] for s in stages})==1
    assert all('parameters' not in s for s in stages)


def test_sampling_failure_logs_sql_stage_and_timeout_without_values(monkeypatch,caplog):
    def fail(body, token=None):
        raise executor.HTTPException(504,{'db_error_code':3024,'diagnostic':{'timeout_origin':'database'}})
    monkeypatch.setattr(executor,'execute_query',fail)
    body=executor.ProfileInput(datasource={'datasource_id':'a','type':'mysql','secret_ref':'DATASOURCE_A_URL'},
        source_table='events',target_table='registry',source_columns=['ref'],target_columns=['code'],
        guards=[{'side':'source','field':'state','value':'private-value'}])
    assert body.sample_rows == 100
    with caplog.at_level(logging.INFO,logger='uvicorn.error'),pytest.raises(executor.HTTPException):
        executor.profile_relationship(body)
    failures=[json.loads(r.message) for r in caplog.records if r.message.startswith('{') and '"status": "failed"' in r.message]
    assert len(failures)==2
    assert {f['direction'] for f in failures}=={'forward','reverse'}
    assert all(f['stage']=='sampling_distinct_random' and f['timeout_ms']==30000 and f['db_error_code']==3024 for f in failures)
    assert 'private-value' not in caplog.text
