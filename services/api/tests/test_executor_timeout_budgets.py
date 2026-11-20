from contextlib import nullcontext

import pytest
from services.executor.app import mysql_adapter
from services.executor.app import main as executor


def test_mysql_metadata_scan_uses_sixty_seconds_for_socket_and_statement(monkeypatch):
    monkeypatch.delenv('SCAN_TIMEOUT_MS', raising=False)
    calls=[]
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self):return nullcontext(self)
        def execute(self,sql,params=()):calls.append((sql,params))
        def fetchall(self):return []
        def rollback(self):pass
        def close(self):pass
    def connect(dsn,timeout):
        assert timeout==60000
        return Connection()
    monkeypatch.setattr(mysql_adapter,'connect',connect)
    monkeypatch.setattr(mysql_adapter,'inspect',lambda dsn:{})
    mysql_adapter.scan('unused',lambda *args:'internal')
    assert ('SET SESSION MAX_EXECUTION_TIME = %s',(60000,)) in calls


@pytest.mark.parametrize('requested,expected',[(None,30000),(30000,30000),(1000,1000),(90000,90000)])
def test_business_queries_use_requested_timeout_and_limits(monkeypatch,requested,expected):
    monkeypatch.delenv('QUERY_TIMEOUT_MS', raising=False)
    captured=[]
    monkeypatch.setattr(executor.adapter_registry,'dialect',lambda body:'oracle')
    monkeypatch.setattr(executor.adapter_registry,'is_jdbc',lambda kind:True)
    monkeypatch.setattr(executor.jdbc_adapter,'settings',lambda body:{})
    def run(*args,**kwargs):
        captured.append((kwargs['timeout_ms'], kwargs['max_rows'], kwargs['max_bytes']))
        return {'columns':['x'],'rows':[[1]]}
    monkeypatch.setattr(executor.jdbc_adapter,'run',run)
    body=executor.QueryInput(sql='SELECT 1 AS x FROM DUAL FETCH FIRST 1 ROWS ONLY',timeout_ms=requested,max_rows=2500,max_bytes=64*1024**2,
        datasource={'datasource_id':'test','type':'oracle','secret_ref':'DATASOURCE_TEST_URL'})
    executor.execute_query(body)
    assert captured==[(expected,2500,64*1024**2)]
