"""Validate model tool envelopes without executing or logging their arguments."""
import json
import re
from copy import deepcopy
from uuid import uuid4

from .model import ModelError


def tool_name(call):
    function=call.get('function') if isinstance(call,dict) else None
    return function.get('name') if isinstance(function,dict) else None


class ToolProtocolError(ModelError):
    def __init__(self,reason,name=None):
        self.reason=reason
        self.returned_name=name if isinstance(name,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}',name) else None
        labels={'invalid_envelope':'工具调用结构无效','unavailable_tool':'工具不在当前阶段允许列表中',
                'invalid_json':'工具参数不是完整JSON','non_object_arguments':'工具参数必须是JSON对象'}
        super().__init__('模型返回的工具调用格式无效：'+labels[reason]+(f'（{self.returned_name}）' if self.returned_name else ''))


def parse_calls(proposed,allowed):
    if not isinstance(proposed,list):raise ToolProtocolError('invalid_envelope')
    calls=[]
    for item in proposed:
        name=tool_name(item)
        if not isinstance(name,str):raise ToolProtocolError('invalid_envelope')
        if name not in allowed:raise ToolProtocolError('unavailable_tool',name)
        function=item['function']
        arguments=function.get('arguments')
        if isinstance(arguments,str):
            try:arguments=json.loads(arguments)
            except ValueError as exc:raise ToolProtocolError('invalid_json',name) from exc
        if not isinstance(arguments,dict):raise ToolProtocolError('non_object_arguments',name)
        call_id=item.get('id')
        if not isinstance(call_id,str) or not call_id.strip():call_id='local_'+uuid4().hex
        calls.append({'name':name,'args':deepcopy(arguments),'id':call_id,'type':'tool_call'})
    return calls
