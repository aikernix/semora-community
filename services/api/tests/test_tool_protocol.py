import asyncio
import json

import pytest
from langchain_core.messages import HumanMessage

from semora_agent.runtime import ToolCallingModel
from semora_agent.model import ModelError
from semora_agent.tool_protocol import parse_calls,ToolProtocolError


TOOLS=[{'type':'function','function':{'name':'AgentSubmission','description':'Submit result',
                                   'parameters':{'type':'object','properties':{'payload':{'type':'object'}}}}}]


def call(name='AgentSubmission',arguments='{"payload":{}}',**kwargs):
    return {'function':{'name':name,'arguments':arguments},**kwargs}


@pytest.mark.parametrize('arguments',['[]','null','1','"text"'])
def test_valid_json_is_not_necessarily_valid_tool_arguments(arguments):
    with pytest.raises(ToolProtocolError) as caught:parse_calls([call(arguments=arguments)],{'AgentSubmission'})
    assert caught.value.reason=='non_object_arguments'


def test_missing_call_id_can_be_generated_without_changing_arguments():
    args={'payload':{'summary':'unchanged','value':'1234567890.1234567890123456789'}}
    result=parse_calls([call(arguments=args)],{'AgentSubmission'})
    assert result[0]['id'].startswith('local_') and result[0]['args']==args
    assert result[0]['args'] is not args


class Client:
    def __init__(self,items):self.items=iter(items);self.requests=[]
    async def complete_messages(self,messages,tools,**kwargs):
        self.requests.append((messages,tools,kwargs))
        return {'content':'','tool_calls':[next(self.items)],'finish_reason':'tool_calls'}


def test_wrong_stage_tool_is_never_executed_and_only_one_protocol_correction_is_allowed():
    async def scenario():
        client=Client([call('validate_intent','{"PRIVATE_ARGUMENT":"do not log"}'),call()]);events=[]
        model=ToolCallingModel(client,lambda n,d:events.append((n,d)),max_models=4,max_tools=3)
        output=await model._agenerate([HumanMessage(content='submit clarification')],tools=TOOLS)
        assert output.generations[0].message.tool_calls[0]['name']=='AgentSubmission'
        assert model._models==2 and model._tools==0 and len(client.requests)==2
        assert client.requests[1][2]['tool_choice']['function']['name']=='AgentSubmission'
        event=next(d for n,d in events if n=='agent.tool.protocol_invalid')
        assert event['reason']=='unavailable_tool' and event['returned_tool']=='validate_intent'
        assert 'PRIVATE_ARGUMENT' not in json.dumps(events)
        assert 'PRIVATE_ARGUMENT' not in json.dumps(client.requests[1][0])
    asyncio.run(scenario())


def test_repeated_invalid_protocol_stops_after_one_correction():
    async def scenario():
        client=Client([call('not_allowed'),call('not_allowed'),call()])
        model=ToolCallingModel(client,lambda *args:None,max_models=5)
        with pytest.raises(ToolProtocolError):
            await model._agenerate([HumanMessage(content='task')],tools=TOOLS)
        assert len(client.requests)==2 and model._tools==0
    asyncio.run(scenario())


def test_protocol_correction_cannot_exceed_model_budget():
    async def scenario():
        client=Client([call('not_allowed'),call()])
        model=ToolCallingModel(client,lambda *args:None,max_models=1)
        with pytest.raises(ToolProtocolError):
            await model._agenerate([HumanMessage(content='task')],tools=TOOLS)
        assert len(client.requests)==1
    asyncio.run(scenario())


def test_truncated_response_is_never_used_as_a_tool_or_retried_as_protocol_fix():
    class Truncated(Client):
        async def complete_messages(self,*args,**kwargs):
            self.requests.append(True)
            return {'tool_calls':[call(arguments='{"payload":')],'finish_reason':'length'}
    async def scenario():
        client=Truncated([]);model=ToolCallingModel(client,lambda *args:None,max_models=4)
        with pytest.raises(ModelError,match='截断'):
            await model._agenerate([HumanMessage(content='task')],tools=TOOLS)
        assert len(client.requests)==1 and model._tools==0
    asyncio.run(scenario())
