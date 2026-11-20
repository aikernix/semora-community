import asyncio
import pytest
from fastapi.testclient import TestClient
from semora_agent.model import ModelError
from semora_agent.runtime import run_agent
from helpers import NativeModel

def test_public_output_overflow_has_only_one_delivery_repair():
    from semora_agent.runtime import ToolCallingModel
    from semora_agent.model_diagnostics import PublicOutputLimitError
    from langchain_core.messages import HumanMessage
    class Overflow:
        def __init__(self): self.calls=0
        async def complete_messages(self,*args,**kwargs):
            self.calls+=1
            raise PublicOutputLimitError('over limit')
    async def scenario():
        client=Overflow();events=[];m=ToolCallingModel(client,lambda n,d:events.append(n),max_models=4)
        m._argument_limit=65536
        with pytest.raises(ModelError,match='限定修订仍未完成'):
            await m._agenerate([HumanMessage(content='review')],tools=[])
        assert client.calls==2 and events.count('agent.delivery.repair')==1
    asyncio.run(scenario())

def test_agent_deadline_uses_configured_budget_and_cancels_pending_model(monkeypatch):
    monkeypatch.setenv('AGENT_TIMEOUT_SECONDS', '0.2')
    cancelled = []

    class SlowModel:
        async def complete_messages(self, *args, **kwargs):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    async def scenario():
        events = []
        with pytest.raises(ModelError, match='Agent 达到 0.2 秒总时限'):
            await run_agent(SlowModel(), '提交结果', {}, [], [], lambda name, data: events.append((name, data)))
        assert cancelled == [True]
        timeout = next(data for name, data in events if name == 'agent.analysis.timeout')
        assert timeout['model_calls'] == 1 and timeout['tool_calls'] == 0
        assert timeout['timeouts'] == 0 and timeout['retries'] == 0

    asyncio.run(scenario())

def test_final_invalid_delivery_stops_with_concrete_error_without_sixth_call():
    from semora_agent.model import PlanValidationError
    async def scenario():
        model=NativeModel([('AgentSubmission',{'payload':{'candidate_id':'invented'}})])
        def invalid(value):raise ValueError('候选未校验通过：具体分组键错误')
        with pytest.raises(PlanValidationError,match='具体分组键错误'):
            await run_agent(model,'test',{},[],[],lambda *a:None,invalid,max_models=1,bounded_protocol=True)
        assert len(model.messages)==1
    asyncio.run(scenario())

def test_protocol_error_after_last_call_never_starts_another_model_request():
    from semora_agent.runtime import ToolCallingModel
    from semora_agent.model import PlanValidationError
    from langchain_core.messages import ToolMessage
    async def scenario():
        client=NativeModel([])
        model=ToolCallingModel(client,lambda *a:None,max_models=1)
        model._models=1
        with pytest.raises(PlanValidationError,match='具体协议错误'):
            await model._agenerate([ToolMessage(content='具体协议错误',name='AgentSubmission',tool_call_id='invalid')],tools=[])
        assert not client.messages and model._models==1
    asyncio.run(scenario())



def test_model_connection_test_requires_real_tool_calling(monkeypatch):
    from app.main import app, model_settings
    class Candidate:
        model = 'test-provider'
        async def complete_json(self, *args):
            return {'ok': True}
        async def complete_messages(self, *args, **kwargs):
            return {'tool_calls': []}
    candidate = Candidate()
    monkeypatch.setattr(model_settings, 'candidate', lambda body: candidate)
    body = {'base_url': 'https://example.com/v1', 'model': 'test', 'api_key': 'test-secret'}
    response = TestClient(app).post('/api/v1/settings/model/test', json=body)
    assert response.status_code == 422 and 'tool calling' in response.json()['detail']
    async def supported(*args, **kwargs):
        return {'tool_calls': [{'function': {'name': 'semora_probe', 'arguments': '{"ok":true}'}}]}
    candidate.complete_messages = supported
    response = TestClient(app).post('/api/v1/settings/model/test', json=body)
    assert response.status_code == 200 and response.json()['tool_calling']
    assert 'test-secret' not in response.text
