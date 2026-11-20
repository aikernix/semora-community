import asyncio
import json

import httpx
import pytest

from semora_agent import model as model_module
from semora_agent.model import ModelError, StructuredModel


def client_with_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(model_module.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    client = StructuredModel()
    client.base_url, client.api_key = 'https://model.example/v1', 'PRIVATE_KEY'
    client.timeout_seconds, client.request_retries = 300, 1
    return client


def test_opt_in_logs_capture_actual_public_requests_and_responses_with_run_correlation(monkeypatch):
    from semora_agent.model_diagnostics import bind_run_log,logger
    from semora_contracts import QueryRun
    monkeypatch.setenv('SEMORA_DEBUG_PAYLOADS','true')
    lines=[];seen=[]
    monkeypatch.setattr(logger,'info',lines.append)
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}',
            'reasoning_content':'PRIVATE_REASONING'},'finish_reason':'stop'}]})
    client=client_with_transport(monkeypatch,handler)
    run=QueryRun(session_id='test',domain='generic',question='Public question')
    counter={}
    async def scenario():
        with bind_run_log(run,counter):
            await client.complete_json('Public instructions','Public question')
        with bind_run_log(run,counter):
            await client.complete_messages([{'role':'user','content':'Follow-up'}],[])
    asyncio.run(scenario())
    records=[json.loads(line) for line in lines]
    requests=[r for r in records if r['event']=='model.http.request']
    responses=[r for r in records if r['event']=='model.http.response']
    assert [r['data']['body'] for r in requests]==seen
    assert [r['data']['http_round'] for r in requests]==[1,2]
    assert all(r['run_id']==run.id and r['trace_id']==run.trace_id for r in requests+responses)
    assert requests[0]['data']['request_id']==responses[0]['data']['request_id']
    assert responses[0]['data']['body']['content']=='{"ok":true}'
    assert 'PRIVATE_KEY' not in ''.join(lines) and 'PRIVATE_REASONING' not in ''.join(lines)
    assert counter['request_count']==2


def test_payload_logging_is_off_by_default_and_scrubs_credentials_when_enabled(monkeypatch):
    from semora_agent.model_diagnostics import bind_run_log,debug_payload,logger
    from semora_contracts import QueryRun
    monkeypatch.delenv('SEMORA_DEBUG_PAYLOADS',raising=False)
    lines=[];monkeypatch.setattr(logger,'info',lines.append)
    run=QueryRun(session_id='test',question='Test')
    with bind_run_log(run,{}):
        debug_payload('test',{'question':'public'})
        assert not lines
        monkeypatch.setenv('SEMORA_DEBUG_PAYLOADS','true')
        debug_payload('test',{'password':'PRIVATE_PASSWORD','api_key':'PRIVATE_KEY',
            'content':'postgresql://user:PRIVATE_DB_PASSWORD@host/db; PRIVATE_RUNTIME_KEY',
            'reasoning_content':'PRIVATE_REASONING'},secrets=['PRIVATE_RUNTIME_KEY'])
    assert len(lines)==1
    assert not any(secret in lines[0] for secret in ['PRIVATE_PASSWORD','PRIVATE_KEY','PRIVATE_DB_PASSWORD','PRIVATE_RUNTIME_KEY','PRIVATE_REASONING'])


@pytest.mark.parametrize('mode,expected',[('',None),('false',False),('true',True)])
@pytest.mark.parametrize('json_completion',[False,True])
def test_thinking_control_applies_to_all_model_request_paths(monkeypatch,mode,expected,json_completion):
    monkeypatch.setenv('LLM_ENABLE_THINKING',mode)
    seen=[];events=[]
    def handler(request):
        body=json.loads(request.content);seen.append(body)
        if expected is None:assert 'enable_thinking' not in body
        else:assert body['enable_thinking'] is expected
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]})
    client=client_with_transport(monkeypatch,handler)
    if json_completion:assert asyncio.run(client.complete_json('system','user'))=={'ok':True}
    else:asyncio.run(client.complete_messages([],[],on_request_event=lambda n,d:events.append((n,d))))
    assert len(seen)==1
    if events:assert events[0][1]['thinking_mode']=={'':'provider_default','false':'disabled','true':'enabled'}[mode]


def test_invalid_thinking_control_fails_before_network(monkeypatch):
    monkeypatch.setenv('LLM_ENABLE_THINKING','typo')
    client=client_with_transport(monkeypatch,lambda request:pytest.fail('unexpected HTTP request'))
    with pytest.raises(ModelError,match='LLM_ENABLE_THINKING'):
        asyncio.run(client.complete_messages([],[]))


@pytest.mark.parametrize('headers_first',[False,True])
def test_no_delivery_deadline_stops_reasoning_or_headers_without_retry(monkeypatch,headers_first):
    from semora_agent.model import NoDeliveryError
    monkeypatch.setenv('LLM_NO_DELIVERY_SECONDS','.05')
    requests=[];events=[];closed=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b'data: {"choices":[{"delta":{"reasoning_content":"PRIVATE_REASONING"}}]}\n\n'
                await asyncio.sleep(.005)
        async def aclose(self):closed.append(True)
    async def handler(request):
        requests.append(True)
        if not headers_first:await asyncio.sleep(10)
        return httpx.Response(200,stream=Stream(),headers={'content-type':'text/event-stream'})
    client=client_with_transport(monkeypatch,handler);client.request_retries=2
    with pytest.raises(NoDeliveryError):
        asyncio.run(client.complete_messages([],[],on_request_event=lambda n,d:events.append((n,d))))
    assert len(requests)==1 and not any(n=='agent.request.retry' for n,_ in events)
    timeout=next(d for n,d in events if n=='agent.request.timeout')
    assert timeout['timeout_kind']=='no_delivery_deadline' and timeout['retryable'] is False
    assert 'PRIVATE_REASONING' not in json.dumps(events)
    if headers_first:assert closed


def test_public_delivery_allows_response_to_finish_after_first_delivery_deadline(monkeypatch):
    monkeypatch.setenv('LLM_NO_DELIVERY_SECONDS','.04')
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"submit","arguments":"{"}}]}}]}\n\n'
            await asyncio.sleep(.08)
            yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n'
    client=client_with_transport(monkeypatch,lambda r:httpx.Response(200,stream=Stream(),headers={'content-type':'text/event-stream'}))
    assert asyncio.run(client.complete_messages([],[]))['tool_calls'][0]['function']['arguments']=='{}'


def test_read_timeout_retries_same_model_request_and_emits_safe_metadata(monkeypatch):
    requests, events = [], []
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            raise httpx.ReadTimeout('PRIVATE_TRANSPORT_DETAIL', request=request)
        return httpx.Response(200, json={'choices': [{'message': {'content': '', 'tool_calls': [], 'reasoning_content': 'PRIVATE_REASONING'}, 'finish_reason': 'stop'}]})
    client = client_with_transport(monkeypatch, handler)
    result = asyncio.run(client.complete_messages([{'role': 'user', 'content': 'PRIVATE_QUESTION'}], [], on_request_event=lambda name, data: events.append((name, data))))
    assert len(requests) == 2 and requests[0] == requests[1]
    assert len([e for e in events if e[0] == 'agent.request.retry']) == 1
    assert events[0][1]['request_bytes'] > 0 and events[0][1]['message_count'] == 1
    assert events[-1][0] == 'agent.request.completed'
    public = json.dumps([events, result])
    assert all(value not in public for value in ['PRIVATE_KEY', 'PRIVATE_QUESTION', 'PRIVATE_REASONING', 'PRIVATE_TRANSPORT_DETAIL'])


def test_exhausted_read_timeout_is_not_reported_as_tool_incompatibility(monkeypatch):
    calls = []
    def handler(request):
        calls.append(True)
        raise httpx.ReadTimeout('timeout', request=request)
    client = client_with_transport(monkeypatch, handler)
    with pytest.raises(ModelError, match='ReadTimeout') as error:
        asyncio.run(client.complete_messages([], []))
    assert len(calls) == 2
    assert '300' in str(error.value) and '已重试 1 次' in str(error.value)
    assert 'tool calling' not in str(error.value)


def test_auth_failure_is_not_retried_or_leaked(monkeypatch):
    calls = []
    def handler(request):
        calls.append(True)
        return httpx.Response(401, json={'error': 'PRIVATE_PROVIDER_BODY'})
    client = client_with_transport(monkeypatch, handler)
    with pytest.raises(ModelError, match='HTTP 401') as error:
        asyncio.run(client.complete_messages([], []))
    assert len(calls) == 1 and 'PRIVATE_PROVIDER_BODY' not in str(error.value)


def test_sse_reassembles_tool_arguments_and_records_counts_without_private_text(monkeypatch,caplog):
    packets=[{'id':'chat-test','choices':[{'index':0,'delta':{'reasoning_content':'PRIVATE_REASONING'}}]},
        {'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'id':'call-1','function':{'name':'submit','arguments':'{\"ok\":'}}]}}]},
        {'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'arguments':'true}'}}]},'finish_reason':'tool_calls'}]},
        {'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':30,'total_tokens':40,'completion_tokens_details':{'reasoning_tokens':20},'private':'PRIVATE_USAGE'}}]
    events=[]
    def handler(request):
        body=json.loads(request.content)
        assert body['stream'] and body['stream_options']['include_usage']
        text=''.join('data: '+json.dumps(p)+'\n\n' for p in packets)+'data: [DONE]\n\n'
        return httpx.Response(200,content=text,headers={'content-type':'text/event-stream','x-request-id':'provider-123'})
    client=client_with_transport(monkeypatch,handler)
    result=asyncio.run(client.complete_messages([],[],on_request_event=lambda n,d:events.append((n,d))))
    assert json.loads(result['tool_calls'][0]['function']['arguments'])=={'ok':True}
    completed=events[-1][1]
    assert completed['reasoning_chars']==len('PRIVATE_REASONING')
    assert completed['reasoning_tokens']==20 and completed['completion_tokens']==30
    assert completed['provider_request_id']=='provider-123'
    assert completed['first_output_ms']>=completed['first_event_ms']
    assert 'PRIVATE_REASONING' not in json.dumps([events,result]) and 'PRIVATE_USAGE' not in json.dumps(events)


def test_stream_truncated_before_finish_never_returns_partial_tool_call(monkeypatch):
    client=client_with_transport(monkeypatch,lambda request:httpx.Response(200,content='data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"x","function":{"name":"execute","arguments":"{"}}]}}]}\n\n',headers={'content-type':'text/event-stream'}))
    with pytest.raises(ModelError,match='响应格式无效'):
        asyncio.run(client.complete_messages([],[]))


def test_cancellation_retains_partial_reasoning_progress_without_content(monkeypatch):
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"reasoning_content":"PRIVATE_REASONING"}}]}\n\n'
            await asyncio.sleep(10)
    events=[]
    client=client_with_transport(monkeypatch,lambda request:httpx.Response(200,stream=SlowStream(),headers={'content-type':'text/event-stream'}))
    async def scenario():
        task=asyncio.create_task(client.complete_messages([],[],on_request_event=lambda n,d:events.append((n,d))))
        while not any(n=='agent.request.reasoning_started' for n,_ in events):await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(scenario())
    assert events[-1][0]=='agent.request.cancelled'
    assert events[-1][1]['stage']=='receiving_reasoning'
    assert events[-1][1]['reasoning_chars']==len('PRIVATE_REASONING')
    assert 'PRIVATE_REASONING' not in json.dumps(events)


def test_request_deadline_stops_active_stream_and_discards_partial_output(monkeypatch):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                await asyncio.sleep(.01)
    client=client_with_transport(monkeypatch,lambda request:httpx.Response(200,stream=Stream(),headers={'content-type':'text/event-stream'}))
    client.timeout_seconds=.05;client.request_retries=0
    events=[]
    with pytest.raises(ModelError,match='ReadTimeout'):
        asyncio.run(client.complete_messages([],[],on_request_event=lambda n,d:events.append((n,d))))
    assert events[-1][1]['timeout_kind']=='request_deadline'
    assert events[-1][1]['output_chars']>0


def test_json_completion_uses_instrumented_stream_without_retaining_reasoning(monkeypatch):
    def handler(request):
        body=json.loads(request.content);assert body['response_format']=={'type':'json_object'}
        return httpx.Response(200,content='data: {"choices":[{"delta":{"content":"{\\\"answer\\\":\\\"ok\\\"}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',headers={'content-type':'text/event-stream'})
    client=client_with_transport(monkeypatch,handler)
    assert asyncio.run(client.complete_json('system','user'))=={'answer':'ok'}


def test_real_http_sse_finish_without_done_returns_and_keeps_usage():
    async def scenario():
        release_connection=asyncio.Event()
        async def handler(reader, writer):
            try:
                headers = (await reader.readuntil(b'\r\n\r\n')).decode()
                length = next(int(x.split(':',1)[1]) for x in headers.split('\r\n') if x.lower().startswith('content-length:'))
                await reader.readexactly(length)
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: keep-alive\r\n\r\n')
                chunks = [
                    {'choices':[{'delta':{'tool_calls':[{'index':0,'id':'call_1','function':{'name':'AgentSubmission','arguments':'{"payload":{"decision":"approved"}}'}}]}}]},
                    {'choices':[{'delta':{},'finish_reason':'tool_calls'}]},
                    {'choices':[], 'usage':{'prompt_tokens':12,'completion_tokens':8}},
                ]
                for chunk in chunks: writer.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                await writer.drain()
                # Keep the socket open until assertions finish; the parser must
                # return on finish_reason, independently of host scheduling.
                await release_connection.wait()
            finally:
                writer.close()
        server = await asyncio.start_server(handler,'127.0.0.1',0)
        client=StructuredModel();client.base_url='http://127.0.0.1:'+str(server.sockets[0].getsockname()[1]);client.api_key='test';client.timeout_seconds=5;client.request_retries=0
        events=[]
        try:
            result=await client.complete_messages([],[],on_request_event=lambda n,d:events.append((n,d)))
            assert result['finish_reason']=='tool_calls'
            assert json.loads(result['tool_calls'][0]['function']['arguments'])['payload']['decision']=='approved'
            completed=events[-1][1]
            assert completed['stream_end']=='finish_reason_grace_elapsed'
            assert completed['prompt_tokens']==12 and completed['completion_tokens']==8
        finally:
            release_connection.set()
            server.close();await server.wait_closed()
    asyncio.run(scenario())


def test_done_without_authoritative_finish_rejects_complete_looking_json(monkeypatch):
    chunks=[{'choices':[{'delta':{'tool_calls':[{'index':0,'id':'x','function':{'name':'AgentSubmission','arguments':'{"payload":{}}'}}]}}]}]
    body=''.join('data: '+json.dumps(x)+'\n\n' for x in chunks)+'data: [DONE]\n\n'
    client=client_with_transport(monkeypatch,lambda request:httpx.Response(200,content=body,headers={'content-type':'text/event-stream'}))
    with pytest.raises(ModelError,match='响应格式无效'):
        asyncio.run(client.complete_messages([],[]))


@pytest.mark.parametrize('second_id,valid',[('call_one',True),('different_call',False)])
def test_repeated_tool_id_is_atomic_and_conflicting_id_is_rejected(monkeypatch,second_id,valid):
    chunks=[{'choices':[{'delta':{'tool_calls':[{'index':0,'id':'call_one','function':{'name':'AgentSubmission','arguments':'{'}}]}}]},
            {'choices':[{'delta':{'tool_calls':[{'index':0,'id':second_id,'function':{'arguments':'}'}}]},'finish_reason':'tool_calls'}]}]
    body=''.join('data: '+json.dumps(x)+'\n\n' for x in chunks)+'data: [DONE]\n\n'
    client=client_with_transport(monkeypatch,lambda request:httpx.Response(200,content=body,headers={'content-type':'text/event-stream'}))
    if valid:
        result=asyncio.run(client.complete_messages([],[]))
        assert result['tool_calls'][0]['id']=='call_one'
        assert result['tool_calls'][0]['function']['arguments']=='{}'
    else:
        with pytest.raises(ModelError,match='响应格式无效'):
            asyncio.run(client.complete_messages([],[]))


def test_public_argument_limit_discards_unfinished_payload_and_logs_only_counts(monkeypatch):
    from semora_agent.model_diagnostics import PublicOutputLimitError
    secret='PRIVATE_PUBLIC_VALUE'
    arguments='{"payload":{"rationale":"'+secret*100+'"}}'
    chunk={'choices':[{'delta':{'tool_calls':[{'index':0,'id':'x','function':{'name':'AgentSubmission','arguments':arguments}}]}}]}
    def handler(request):
        assert '_semora_tool_argument_limit' not in json.loads(request.content)
        return httpx.Response(200,content='data: '+json.dumps(chunk)+'\n\n',headers={'content-type':'text/event-stream'})
    client=client_with_transport(monkeypatch,handler);events=[]
    with pytest.raises(PublicOutputLimitError):
        asyncio.run(client.complete_messages([],[],max_tool_argument_chars=1000,on_request_event=lambda n,d:events.append((n,d))))
    limited=next(d for n,d in events if n=='agent.request.output_limit')
    assert limited['failure_kind']=='public_output_limit' and limited['argument_chars']>1000
    assert limited['repeated_blocks']>0 and secret not in json.dumps(events)


def test_local_relation_adapter_omits_optional_parameters_without_affecting_queries(monkeypatch):
    from app.relationship_context import complete
    monkeypatch.setenv('RELATIONSHIP_RESPONSE_FORMAT','text')
    monkeypatch.setenv('RELATIONSHIP_THINKING_MODE','omit')
    monkeypatch.setenv('LLM_ENABLE_THINKING','false')
    seen=[]
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]})
    client=client_with_transport(monkeypatch,handler)
    async def run():
        assert await complete(client,'generic',{'tables':[]})=={'ok':True}
        assert await client.complete_json('generic','data')=={'ok':True}
    asyncio.run(run())
    assert 'response_format' not in seen[0] and 'enable_thinking' not in seen[0]
    assert seen[1]['response_format']=={'type':'json_object'}
    assert seen[1]['enable_thinking'] is False


def test_relation_adapter_preserves_current_json_mode_and_disabled_thinking(monkeypatch):
    from app.relationship_context import complete
    monkeypatch.setenv('RELATIONSHIP_RESPONSE_FORMAT','json_object')
    monkeypatch.setenv('RELATIONSHIP_THINKING_MODE','inherit')
    monkeypatch.setenv('LLM_ENABLE_THINKING','false')
    def handler(request):
        body=json.loads(request.content)
        assert body['response_format']=={'type':'json_object'}
        assert body['enable_thinking'] is False
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]})
    client=client_with_transport(monkeypatch,handler)
    assert asyncio.run(complete(client,'generic',{'tables':[]}))=={'ok':True}
