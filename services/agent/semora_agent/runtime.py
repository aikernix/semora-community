from __future__ import annotations
from .prompts import FINAL_DELIVERY, SCHEMA_RETRY, COMPACT_OUTPUT

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict, messages_to_dict
from langchain_core.messages.utils import convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from .model import ModelError, PlanValidationError
from .context_budget import prepare_context,ContextBudgetError
from .model_diagnostics import PublicOutputLimitError
from .progress import ProgressBudget, NoProgressError
from .tool_protocol import parse_calls,tool_name,ToolProtocolError


class AgentSubmission(BaseModel):
    """Deliver the final public analysis; never include hidden reasoning."""

    payload: dict[str, Any] = Field(description='结构化分析结果，必须满足当前输出契约')


class ToolCallingModel(BaseChatModel):
    """Adapt the existing configured provider without another SDK or credentials store."""

    # Cython methods have their own callable type; they remain methods, not fields.
    model_config = ConfigDict(ignored_types=(type(lambda: None),))

    _client: Any = PrivateAttr()
    _event: Callable = PrivateAttr()
    _models: int = PrivateAttr(default=0)
    _tools: int = PrivateAttr(default=0)
    _force_submission: bool = PrivateAttr(default=False)
    _max_models: int = PrivateAttr(default=16)
    _max_tools: int = PrivateAttr(default=12)
    _argument_limit: int | None = PrivateAttr(default=None)
    _output_repairs: int = PrivateAttr(default=0)
    _protocol_repairs: int = PrivateAttr(default=0)
    _progress: Any = PrivateAttr(default=None)
    _bounded_protocol: bool = PrivateAttr(default=False)
    _track_progress: bool = PrivateAttr(default=False)
    _submission_repairs: int = PrivateAttr(default=0)
    _task_guard: Any = PrivateAttr(default=None)

    def __init__(self, client, event: Callable, max_models=16, max_tools=12):
        super().__init__()
        self._client = client
        self._event = event
        self._max_models, self._max_tools = max_models, max_tools
        self._progress=ProgressBudget(lambda n,d:self._event(n,d))

    @property
    def _llm_type(self):
        return 'semora-tool-calling'

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(item) for item in tools], **kwargs)

    def _generate(self, *args, **kwargs):
        raise NotImplementedError('Use async Agent execution')

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self._task_guard:self._task_guard()
        if self._bounded_protocol and not self._track_progress:
            # This controller permits only bounded preparation/validation;
            # repeated metadata is not a semantic failure signal here.
            self._progress.pending=None
        else:self._progress.consume(messages)
        self._progress.check()
        if self._models >= self._max_models:
            reason='最后一次交付未满足结构化协议，尚无有效结果'
            for message in reversed(messages):
                if getattr(message,'type',None)=='tool' and getattr(message,'name',None) in {'AgentSubmission'}:
                    reason=str(message.content)[:1000];break
            raise PlanValidationError(f'当前阶段已完成 {self._max_models} 次模型调用，停止继续循环；未解决问题：{reason}')
        self._models += 1
        self._event('agent.model.started', {'iteration': self._models, 'summary': f'第 {self._models} 轮分析：根据已有证据选择下一步'})
        choice = kwargs.get('tool_choice')
        if choice == 'any':
            choice = 'required'
        if self._force_submission or self._models >= self._max_models or self._tools >= self._max_tools:
            choice = {'type': 'function', 'function': {'name': 'AgentSubmission'}}
            self._force_submission = False
        request_tools = kwargs.get('tools', [])
        if self._bounded_protocol and self._models>=self._max_models:
            request_tools=[t for t in request_tools if t['function']['name']=='AgentSubmission']
            messages=[*messages,HumanMessage(content=FINAL_DELIVERY)]
        if self._progress.must_deliver and (not self._bounded_protocol or self._track_progress):
            request_tools=[t for t in request_tools if t['function']['name']=='AgentSubmission']
            choice={'type':'function','function':{'name':'AgentSubmission'}}
            messages=[*messages,HumanMessage(content=FINAL_DELIVERY)]
        try:
            request_messages, context_stats = prepare_context(convert_to_openai_messages(messages), request_tools)
        except ContextBudgetError as exc:
            self._event('agent.context.exceeded',{**exc.stats,'summary':'请求超过上下文预算，尚未发起模型HTTP请求'})
            raise
        self._event('agent.context.prepared', {**context_stats, 'summary': '已无损精简上下文，完整保留需求、约束和证据；重复工具结果引用原文'})
        try:
            options = {'max_tool_argument_chars': self._argument_limit} if self._argument_limit else {}
            raw = await self._client.complete_messages(request_messages, request_tools, tool_choice=choice, parallel_tool_calls=False, on_request_event=self._event, **options)
            if self._task_guard:self._task_guard()
        except PublicOutputLimitError as exc:
            if self._output_repairs or self._models >= self._max_models:
                raise ModelError('Reviewer 结构化交付超限，限定修订仍未完成；未执行部分产物') from exc
            self._output_repairs += 1
            self._force_submission = True
            self._event('agent.delivery.repair', {'failure_kind':'public_output_limit','summary':'复核交付超限，丢弃未终止内容；仅允许一次精简交付，仍须完整校验'})
            return await self._agenerate(messages+[HumanMessage(content=COMPACT_OUTPUT)],stop,run_manager,**kwargs)
        proposed = raw.get('tool_calls') or []
        if isinstance(proposed,list) and len(proposed) > 1:
            # Some compatible providers ignore parallel_tool_calls=False.
            # Execute one observation-producing action and let the next model
            # turn choose again, instead of running a speculative batch.
            selected = next((c for c in proposed if tool_name(c) != 'AgentSubmission'), proposed[0])
            self._event('agent.actions.deferred', {'summary': f'本轮提出 {len(proposed)} 个动作，仅执行一个；其余需根据结果在后续轮次重新选择', 'selected_tool': tool_name(selected)})
            proposed = [selected]
        self._event('agent.model.completed', {'iteration': self._models, 'summary': f'模型返回 {len(proposed)} 个工具动作' if isinstance(proposed,list) and proposed else '模型返回需要检查的交付', 'finish_reason': raw.get('finish_reason')})
        if raw.get('finish_reason') == 'length':
            raise ModelError('模型输出被长度上限截断，无法形成完整产物；请缩小需求或调整模型输出限制')
        allowed={t['function']['name'] for t in request_tools}
        try:
            calls=parse_calls(proposed,allowed)
        except ToolProtocolError as exc:
            self._event('agent.tool.protocol_invalid',{'reason':exc.reason,'returned_tool':exc.returned_name,
                'allowed_tools':sorted(allowed),'summary':str(exc),'executed':False})
            if self._protocol_repairs or self._models>=self._max_models:raise
            self._protocol_repairs+=1
            self._progress.fail('invalid_tool_protocol')
            options=dict(kwargs)
            target=next(iter(allowed)) if len(allowed)==1 else (exc.returned_name if exc.returned_name in allowed else None)
            options['tool_choice']={'type':'function','function':{'name':target}} if target else 'required'
            feedback={'tool_protocol_error':exc.reason,'allowed_tools':sorted(allowed),
                      'action':'只调用当前允许的工具，参数必须是完整JSON对象；保留原任务与已确认内容。'}
            self._event('agent.tool.protocol_repair',{'summary':'未执行无效工具调用；仅在剩余预算内尝试一次协议补正'})
            return await self._agenerate(messages+[HumanMessage(content=json.dumps(feedback,ensure_ascii=False))],stop,run_manager,**options)
        self._tools+=sum(c['name']!='AgentSubmission' for c in calls)
        if self._tools > self._max_tools:
            raise ModelError(f'Agent 已达到 {self._max_tools} 次工具调用上限，尚未解决的问题需要补充说明')
        # Do not retain free-form model text when it is making tool calls.
        message = AIMessage(content='' if calls else str(raw.get('content') or ''), tool_calls=calls)
        self._progress.pending=calls
        return ChatResult(generations=[ChatGeneration(message=message)])


async def run_agent(client, system: str, context: dict, tools: list, memory: list,
                    event: Callable, validate: Callable | None = None,
                    submission_schema: type[AgentSubmission] = AgentSubmission,
                    max_models: int = 16, max_tools: int = 12, max_tool_argument_chars=None, progress_state=None, bounded_protocol=False, task_guard=None, track_progress=False) -> tuple[dict, list]:
    model = ToolCallingModel(client, event, max_models, max_tools)
    model._argument_limit = max_tool_argument_chars
    model._progress=ProgressBudget(lambda n,d:model._event(n,d),progress_state)
    model._bounded_protocol=bounded_protocol
    model._track_progress=track_progress
    model._task_guard=task_guard
    # The outer workflow owns durable checkpoints. Keep the transient tool
    # graph isolated; only JSON-safe, provider-filtered messages go into Run.
    def repair_submission(error):
        # Format repair is distinct from exploratory progress. A first schema
        # error must not exhaust a budget previously spent on empty lookups.
        model._progress.pending = None
        model._submission_repairs += 1
        cause = getattr(error, 'source', error)
        seen = set()
        while not isinstance(cause, ValidationError) and id(cause) not in seen:
            seen.add(id(cause))
            nested = getattr(cause, '__cause__', None) or getattr(cause, '__context__', None)
            if nested is None: break
            cause = nested
        errors = [{'path': '.'.join(map(str, e['loc'])), 'type': e['type'], 'message': e['msg']}
                  for e in cause.errors(include_input=False, include_url=False)][:12] if isinstance(cause, ValidationError) else [
                      {'path': 'payload', 'type': type(cause).__name__, 'message': '结构化交付不满足输出契约'}]
        model._event('agent.submission.invalid', {'summary': '结构化交付格式未通过，正在按具体字段补正',
            'errors': errors, 'repair_attempt': model._submission_repairs})
        if model._submission_repairs > 2 or model._models >= model._max_models:
            raise PlanValidationError('结构化交付格式补正仍未完成：'+'；'.join(e['path']+': '+e['message'] for e in errors))
        model._force_submission = True
        return json.dumps({'schema_errors': errors, 'action': '按当前AgentSubmission Schema补正格式，保留已取得的资料与方案，不重新检索。'}, ensure_ascii=False)
    agent = create_agent(model=model, tools=tools, system_prompt=system, checkpointer=False,
                         response_format=ToolStrategy(submission_schema, handle_errors=repair_submission if track_progress else True))
    messages = messages_from_dict(memory) if memory else []
    messages.append(HumanMessage(content=json.dumps(context, ensure_ascii=False, default=str)))
    timeout_seconds = float(os.getenv('AGENT_TIMEOUT_SECONDS', '900'))
    request_stats = {'duration_ms': 0, 'timeouts': 0, 'retries': 0}
    original_event = model._event
    def observe(name, data):
        if name=='agent.request.started':model._progress.state['http_attempts']+=1
        if name in {'agent.request.timeout','agent.request.failed'}:
            model._progress.fail(data.get('timeout_kind') or 'model_request_failed')
        if name in {'agent.request.completed', 'agent.request.timeout', 'agent.request.cancelled', 'agent.request.failed'}:
            request_stats['duration_ms'] += data.get('duration_ms', 0)
        if name == 'agent.request.timeout':
            request_stats['timeouts'] += 1
        if name == 'agent.request.retry':
            request_stats['retries'] += 1
        original_event(name, data)
    model._event = observe
    try:
        async with asyncio.timeout(timeout_seconds):
            validation_seen = set()
            validation_streak = 0
            for attempt in range(max_models if track_progress else 3):
                result = await agent.ainvoke({'messages': messages}, config={'recursion_limit': max(40, 2 * (max_models + max_tools) + 10)})
                submission = result.get('structured_response')
                if not isinstance(submission, AgentSubmission):
                    try:
                        model._progress.consume(result.get('messages',messages))
                    except NoProgressError as stopped:
                        raise NoProgressError(f'模型连续 {model._progress.limit} 次未按结构化交付协议提交结果，已停止后续步骤；已调用过工具不代表最终交付一定合规') from stopped
                    if not track_progress and attempt == 2:
                        raise ModelError('模型连续 3 次未按结构化交付协议提交结果，已停止后续步骤；已调用过工具不代表最终交付一定合规')
                    event('agent.output.retry', {'summary': '模型尚未提交结构化结果，保留已取得的工具观察并要求重新提交', 'attempt': attempt + 1})
                    model._force_submission = True
                    messages = result.get('messages', messages) + [HumanMessage(content=SCHEMA_RETRY)]
                    continue
                payload = submission.payload.model_dump(mode='json') if isinstance(submission.payload, BaseModel) else submission.payload
                if task_guard:task_guard()
                try:
                    if validate:
                        validate(payload)
                    model._progress.pending=None
                    model._progress.success('validated_delivery')
                    break
                except ValueError as exc:
                    if model._models >= model._max_models:
                        raise PlanValidationError(f'本轮修订预算已用尽，尚未通过校验：{exc}。需求与已确认口径已保留；未执行无效方案。') from exc
                    event('agent.validation.failed', {'summary': f'方案校验未通过，正在修正：{str(exc)[:1000]}', 'attempt': attempt + 1})
                    if track_progress:
                        model._progress.pending = None
                        model._force_submission = True
                        signature = str(exc)
                        validation_streak = validation_streak + 1 if signature in validation_seen else 0
                        validation_seen.add(signature)
                        if validation_streak >= model._progress.limit:
                            raise PlanValidationError('同一类方案问题反复未修正，已停止重复处理：'+str(exc)) from exc
                    else:
                        try:
                            model._progress.reject_submission()
                        except NoProgressError as stopped:
                            raise PlanValidationError(f'连续无进展，停止分析：{exc}') from stopped
                    if not track_progress and attempt == 2:
                        raise PlanValidationError(f'Agent 连续 3 次提交无效方案：{exc}') from exc
                    messages = result['messages'] + [HumanMessage(content=json.dumps({'validation_error':str(exc)[:1200],'action':'correct_current_submission'},ensure_ascii=False))]
            else:
                raise PlanValidationError('当前交付预算内未能形成有效结构化结论；已有需求与资料已保留。')
    except TimeoutError as exc:
        summary = (f'Agent 达到 {timeout_seconds:g} 秒总时限；已发起 {model._models} 轮模型调用、'
                   f'{model._tools} 次工具调用，模型请求超时 {request_stats["timeouts"]} 次、'
                   f'重试 {request_stats["retries"]} 次。已停止本次分析，尚未形成可执行结论。')
        event('agent.analysis.timeout', {'summary': summary, 'model_calls': model._models,
                                       'tool_calls': model._tools, **request_stats})
        raise ModelError(summary) from exc
    event('agent.analysis.completed', {'summary': '已提交结构化分析结果', 'model_calls': model._models, 'tool_calls': model._tools})
    return payload, messages_to_dict(result['messages'])
