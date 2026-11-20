from __future__ import annotations

import asyncio
from contextlib import suppress
from .model_diagnostics import RequestDiagnostics, read_response, await_public_delivery, NoDeliveryTimeoutError, debug_payload,log_context,next_http_round

import json
import os
import re
from typing import Any

import httpx


class ModelError(RuntimeError):
    pass


class ModelServiceError(ModelError):
    """Provider/transport failure, distinct from application configuration bugs."""








class TaskSupersededError(ModelError):
    """A stale response must not replace a newer authoritative task input."""


class NoDeliveryError(ModelError):
    public_message='模型在交付截止时间内未返回答案或工具参数，已停止本次分析；需求与已确认口径已保留。'




class PlanValidationError(ModelError):
    """Keep diagnostic details for callers, with a separate user-facing message."""
    public_message = '模型暂未能将已确认口径完整落实为可执行方案，已停止本次分析。请重新分析，详细原因可在分析记录中查看。'


class StructuredModel:
    """Compatible model client; only reasoning counts, never reasoning text, are retained."""

    def __init__(self) -> None:
        self.base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")
        self.mode = os.getenv("LLM_MODE", "optional").lower()
        self.timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS", "300"))
        self.request_retries = max(0, min(2, int(os.getenv('LLM_REQUEST_RETRIES', '1'))))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def required(self) -> bool:
        return self.mode == "required"

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    async def complete_json(self, system: str, user: str, *, on_request_event=None) -> dict[str, Any]:
        if not self.configured:
            raise ModelError("LLM 未配置")
        from .context_budget import prepare_context
        messages,_=prepare_context([{'role':'system','content':system},{'role':'user','content':user}],[])
        from .model_diagnostics import current_json_options
        options=current_json_options()
        body={'model':self.model,'temperature':0.3,'messages':messages}
        if not options or options[0]=='json_object':
            body['response_format']={'type':'json_object'}
        raw = await self._request(body,on_request_event)
        content = raw.get('content')
        if not isinstance(content, str):
            raise ModelError("模型未返回 JSON 文本")
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelError("模型返回的 JSON 无法解析") from exc
        if not isinstance(value, dict):
            raise ModelError("模型返回必须是 JSON 对象")
        return value

    async def complete_messages(self, messages: list[dict], tools: list[dict], **kwargs: Any) -> dict:
        """Native tool calling; do not request or forward model-private reasoning."""
        if not self.configured:
            raise ModelError('LLM 未配置')
        body = {'model': self.model, 'temperature': 0.3, 'messages': messages, 'tools': tools}
        if kwargs.get('tool_choice') is not None:
            body['tool_choice'] = kwargs['tool_choice']
        if kwargs.get('parallel_tool_calls') is not None:
            body['parallel_tool_calls'] = kwargs['parallel_tool_calls']
        if kwargs.get('max_tool_argument_chars'):
            # Internal transport policy, never forwarded to provider APIs.
            body['_semora_tool_argument_limit']=kwargs['max_tool_argument_chars']
        return await self._request(body, kwargs.get('on_request_event'))

    async def _request(self, body, callback=None):
        body=dict(body)
        argument_limit=body.pop('_semora_tool_argument_limit',None)
        thinking=os.getenv('LLM_ENABLE_THINKING','').strip().lower()
        from .model_diagnostics import current_json_options
        options=current_json_options()
        if options and options[1]!='inherit':
            thinking='' if options[1]=='omit' else options[1]
        from .model_diagnostics import current_delivery_budget
        scoped_budget = current_delivery_budget()
        delivery_seconds=float(scoped_budget[0] if scoped_budget else os.getenv('LLM_NO_DELIVERY_SECONDS','60'))
        if delivery_seconds < 0:
            raise ModelError('LLM_NO_DELIVERY_SECONDS 不能为负数')
        if thinking not in {'','true','false'}:
            raise ModelError('LLM_ENABLE_THINKING 必须为空、true 或 false')
        if thinking:
            # httpx sends provider extensions directly in the JSON body.
            # Empty preserves compatibility with providers lacking this option.
            body['enable_thinking']=thinking=='true'
        streaming=os.getenv('LLM_STREAM_ENABLED','true').lower()=='true'
        if streaming:
            body={**body,'stream':True}
            if os.getenv('LLM_STREAM_INCLUDE_USAGE','true').lower()=='true':
                body['stream_options']={'include_usage':True}
        callback=callback or (lambda *args:None)
        metadata={'model':self.model,'message_count':len(body['messages']),'tool_count':len(body.get('tools',[])),
                  'stream_requested':streaming,'request_bytes':len(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode('utf-8')),
                  'request_timeout_seconds':self.timeout_seconds,'thinking_mode':{'true':'enabled','false':'disabled','':'provider_default'}[thinking],
                  'max_tool_argument_chars':argument_limit}
        metadata['no_delivery_seconds']=delivery_seconds
        for attempt in range(self.request_retries+1):
            diagnostic=RequestDiagnostics({**metadata,**log_context(),'http_round':next_http_round(),'attempt':attempt+1},callback)
            debug_payload('model.http.request',{'request_id':diagnostic.data['diagnostic_id'],
                'http_round':diagnostic.data['http_round'],'attempt':attempt+1,'body':body},secrets=[self.api_key])
            diagnostic.emit('agent.request.started',f'正在请求模型，第 {attempt+1} 次尝试；约 {metadata["request_bytes"]} 字节')
            heartbeat=asyncio.create_task(diagnostic.heartbeat())
            try:
                # Total request deadline remains 300s even if SSE keeps arriving.
                async with asyncio.timeout(self.timeout_seconds):
                    async def send_and_read():
                        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                            async with client.stream('POST',self.endpoint,headers={'Authorization':f'Bearer {self.api_key}'},json=body,extensions={'trace':diagnostic.transport_trace}) as response:
                                diagnostic.headers(response)
                                response.raise_for_status()
                                return await read_response(response,diagnostic)
                    result=await await_public_delivery(send_and_read(),diagnostic,delivery_seconds)
                diagnostic.data['stage']='completed'
                debug_payload('model.http.response',{'request_id':diagnostic.data['diagnostic_id'],
                    'http_round':diagnostic.data['http_round'],'duration_ms':diagnostic.elapsed(),'body':result},secrets=[self.api_key])
                diagnostic.emit('agent.request.completed',f'模型请求完成：总耗时 {diagnostic.elapsed()/1000:.1f} 秒；首个响应事件 {diagnostic.data.get("first_event_ms", "未知")} ms；推理字符 {diagnostic.data["reasoning_chars"]}，输出字符 {diagnostic.data["output_chars"]}，工具参数字符 {diagnostic.data["tool_argument_chars"]}；推理token {diagnostic.data.get("reasoning_tokens", "服务未返回")}，输出token {diagnostic.data.get("completion_tokens", "服务未返回")}')
                return result
            except NoDeliveryTimeoutError as exc:
                diagnostic.data['timeout_kind']='no_delivery_deadline'
                diagnostic.data['retryable']=False
                diagnostic.emit('agent.request.timeout',f'模型 {delivery_seconds:g} 秒未交付答案或工具参数，停止本次请求，不自动重试；部分产物不执行')
                raise NoDeliveryError(scoped_budget[1] if scoped_budget else NoDeliveryError.public_message) from exc
            except (httpx.ReadTimeout,TimeoutError) as exc:
                diagnostic.data['timeout_kind']='read_idle' if isinstance(exc,httpx.ReadTimeout) else 'request_deadline'
                diagnostic.emit('agent.request.timeout',f'模型请求达到 {self.timeout_seconds:g} 秒等待限制；已保留响应进度')
                if attempt<self.request_retries:
                    diagnostic.data['next_attempt']=attempt+2
                    diagnostic.emit('agent.request.retry',f'模型请求超时，正在进行第 {attempt+1}/{self.request_retries} 次重试；部分输出不执行')
                    continue
                raise ModelServiceError(f'模型响应读取超时（ReadTimeout）：单次等待上限 {self.timeout_seconds:g} 秒，已重试 {self.request_retries} 次；请查看请求阶段及响应进度') from exc
            except asyncio.CancelledError:
                diagnostic.emit('agent.request.cancelled','请求被上层取消（可能是Agent总时限或用户停止），已保留响应进度')
                raise
            except httpx.HTTPStatusError as exc:
                diagnostic.emit('agent.request.failed',f'模型服务返回 HTTP {exc.response.status_code}')
                raise ModelServiceError(f'模型服务返回 HTTP {exc.response.status_code}；请检查服务状态、认证、额度和接口参数') from exc
            except httpx.RequestError as exc:
                diagnostic.data['error_type']=type(exc).__name__
                diagnostic.emit('agent.request.failed',f'模型网络请求失败：{type(exc).__name__}')
                raise ModelServiceError(f'模型网络请求失败（{type(exc).__name__}），请检查网络与模型服务连接') from exc
            except (KeyError,IndexError,TypeError,ValueError) as exc:
                diagnostic.emit('agent.request.failed','模型响应格式无效或流未完整结束；部分输出不执行')
                raise ModelError(f'模型响应格式无效（{type(exc).__name__}），未取得有效的消息结构') from exc
            finally:
                if diagnostic.data['stage']!='completed':
                    debug_payload('model.http.incomplete',{'request_id':diagnostic.data['diagnostic_id'],
                        'http_round':diagnostic.data['http_round'],'diagnostics':diagnostic.data})
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):await heartbeat


structured_model = StructuredModel()
