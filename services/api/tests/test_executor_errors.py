import asyncio

import httpx
import pytest

from services.executor.app.errors import database_error
from app.executor_errors import ExecutorOperationError,check_response,request_executor


@pytest.mark.parametrize('code,kind',[(1064,'syntax'),(1054,'schema'),(1045,'auth'),(1142,'permission'),(2003,'connect'),(1235,'unsupported_sql')])
def test_mysql_native_codes_are_classified_without_leaking_driver_message(code,kind):
    error=database_error(Exception(code,'PRIVATE_PASSWORD and SQL literal'),'estimate')
    assert error.detail['error_type']==kind and error.detail['db_error_code']==code
    assert 'PRIVATE' not in str(error.detail)


def test_psycopg3_sqlstate_and_unknown_error_are_preserved_safely():
    class PgError(Exception):sqlstate='42703'
    assert database_error(PgError('SECRET'),'estimate').detail['error_type']=='schema'
    assert database_error(TypeError('SECRET'),'estimate').detail['error_type']=='unknown'


@pytest.mark.parametrize('detail,expected',[
    ({'error_type':'syntax','db_error_code':1064},'syntax'),
    ('MySQL failure','unknown'),
    ({'error_type':'capability'},'unknown'),
    ({'error_type':'capability','operation':'estimate'},'capability')])
def test_http_502_does_not_imply_transient_or_estimate_capability(detail,expected):
    response=httpx.Response(502,json={'detail':detail})
    with pytest.raises(ExecutorOperationError) as caught:check_response(response,'estimate')
    assert caught.value.kind==expected






def test_transport_error_is_typed_without_retrying_query_implicitly():
    async def scenario():
        calls=[]
        async def handler(request):calls.append(True);raise httpx.ConnectError('PRIVATE',request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ExecutorOperationError) as caught:
                await request_executor(client,'http://executor/internal/v1/query',operation='query',json={})
        assert caught.value.kind=='connect' and len(calls)==1 and 'PRIVATE' not in str(caught.value)
    asyncio.run(scenario())


def test_database_and_system_deadlines_have_distinct_origins():
    from app.executor_errors import transport_error
    error=database_error(Exception(3024,'PRIVATE'),'query')
    with pytest.raises(ExecutorOperationError) as caught:
        check_response(httpx.Response(502,json={'detail':error.detail}),'query')
    assert caught.value.public()['timeout_origin']=='database'
    assert caught.value.retryable
    assert transport_error(httpx.ReadTimeout('PRIVATE')).public()['timeout_origin']=='system'


def test_sort_abort_is_only_a_timeout_when_database_explicitly_reports_deadline():
    timeout=database_error(Exception(1028,'Sort aborted: Query execution was interrupted, maximum statement execution time exceeded'),'query')
    assert timeout.detail['error_type']=='timeout'
    other=database_error(Exception(1028,'Sort aborted: disk failure'),'query')
    assert other.detail['error_type']=='unknown'


@pytest.mark.parametrize('state',['57014','HYT00','HYT01'])
def test_database_timeout_sqlstates_are_preserved(state):
    error=Exception('PRIVATE');error.sqlstate=state
    detail=database_error(error,'query').detail
    assert detail['error_type']=='timeout' and detail['diagnostic']['timeout_origin']=='database'


def test_jdbc_timeout_exception_without_sqlstate_is_classified():
    error=Exception(0);error.is_sql_timeout=True
    assert database_error(error,'query').detail['error_type']=='timeout'


def test_cancelled_statement_stops_worker_before_retry(monkeypatch):
    from app.executor_client import ExecutorClient
    real_client=httpx.AsyncClient
    calls=[]
    async def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/query'):raise asyncio.CancelledError()
        return httpx.Response(200,json={'accepted':True})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(handler),**kwargs))
    monkeypatch.setattr(ExecutorClient,'datasource_payload',lambda *args:{'datasource_id':'a','type':'mysql','secret_ref':'NOT_A_SECRET'})
    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await ExecutorClient().execute('SELECT 1',{},[],{'execution_id':'attempt','query_timeout_seconds':30,
                'max_result_rows':1000,'max_result_mb':10,'data_source_id':'a'})
    asyncio.run(scenario())
    assert calls==['/internal/v1/query','/internal/v1/executions/attempt/cancel']
