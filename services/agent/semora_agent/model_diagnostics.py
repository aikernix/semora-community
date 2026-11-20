"""Operational metadata and opt-in public request/response debugging."""
import asyncio
import json
import logging
import re
import time
import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

logger = logging.getLogger('semora.model')
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.propagate = False

_run_log=ContextVar('semora_run_log',default=None)


@contextmanager
def bind_run_log(run,state):
    token=_run_log.set({'run_id':run.id,'trace_id':run.trace_id,'session_id':run.session_id,'domain':run.domain,'counter':state})
    try:yield
    finally:_run_log.reset(token)


def log_context():
    return {k:v for k,v in (_run_log.get() or {}).items() if k!='counter'}


def next_http_round():
    context=_run_log.get()
    if context is None:return None
    counter=context['counter']
    counter['request_count']=counter.get('request_count',0)+1
    return counter['request_count']


def debug_payload(event,data,*,secrets=()):
    """Docker stdout only: never add large payloads to Agent memory/events."""
    if os.getenv('SEMORA_DEBUG_PAYLOADS','false').lower()!='true' or _run_log.get() is None:return
    known=set(s for s in secrets if isinstance(s,str) and s)
    known.update(v for k,v in os.environ.items() if len(v)>=8 and re.search(r'(PASSWORD|API_KEY|TOKEN|SECRET|DATABASE_URL|DATASOURCE.*URL)',k))
    hidden={'authorization','api_key','password','passwd','access_token','client_secret','connection_string','dsn'}
    def clean(value):
        if isinstance(value,dict):
            return {k:('[REDACTED]' if str(k).lower().replace('-','_') in hidden else clean(v))
                    for k,v in value.items() if k not in {'reasoning_content','reasoning_details'}}
        if isinstance(value,(list,tuple)):return [clean(v) for v in value]
        if isinstance(value,str):
            for secret in sorted(known,key=len,reverse=True):value=value.replace(secret,'[REDACTED]')
            return re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:)[^\s@]+@',r'\1[REDACTED]@',value)
        return value
    try:
        logger.info(json.dumps(clean({'event':event,**log_context(),'data':data}),ensure_ascii=False,default=str))
    except Exception:
        logger.warning('semora.debug_payload.unavailable')


class PublicOutputLimitError(RuntimeError):
    """A public tool payload exceeded its stage contract; never use its prefix."""


class NoDeliveryTimeoutError(TimeoutError):
    """No public answer or tool arguments arrived before the delivery deadline."""


async def await_public_delivery(request, diagnostics, seconds):
    """Bound headers + reasoning-only streaming, without restarting the request."""
    if seconds <= 0:
        return await request
    work=asyncio.create_task(request)
    delivery=asyncio.create_task(diagnostics.delivery_started.wait())
    try:
        done,_=await asyncio.wait({work,delivery},timeout=seconds,return_when=asyncio.FIRST_COMPLETED)
        if work in done or delivery in done:
            return await work
        raise NoDeliveryTimeoutError('no public model delivery')
    finally:
        for task in (work,delivery):
            if not task.done():task.cancel()
        await asyncio.gather(work,delivery,return_exceptions=True)


def public_argument_stats(value):
    # Public tool JSON only. Retain structural counts, never values or reasoning.
    windows=[value[i:i+256] for i in range(0,len(value)-255,256)]
    hashes=[hashlib.sha256(w.encode()).digest() for w in windows]
    duplicates=0
    def count_keys(pairs):
        nonlocal duplicates
        duplicates+=len(pairs)-len({k for k,_ in pairs})
        return dict(pairs)
    valid=True
    try: json.loads(value,object_pairs_hook=count_keys)
    except (ValueError,RecursionError):valid=False
    return {'argument_chars':len(value),'repeated_blocks':len(hashes)-len(set(hashes)),
            'argument_json_valid':valid,'duplicate_json_keys':duplicates,
            'resolution_keys':len(re.findall(r'"finding_id"\s*:',value)),
            'issue_keys':len(re.findall(r'"issues"\s*:',value)),
            'rationale_keys':len(re.findall(r'"rationale"\s*:',value))}


def safe_id(value):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}',value) else None


class RequestDiagnostics:
    def __init__(self, metadata, callback):
        self.started=time.monotonic()
        self.callback=callback
        self.delivery_started=asyncio.Event()
        self.data={**metadata,'diagnostic_id':str(uuid4()),'stage':'waiting_headers',
                   'chunks':0,'reasoning_chars':0,'output_chars':0,'tool_argument_chars':0}

    def elapsed(self): return round((time.monotonic()-self.started)*1000)

    def emit(self,name,summary):
        value={**self.data,'duration_ms':self.elapsed(),'summary':summary}
        logger.info(json.dumps({'event':name,**value},ensure_ascii=False))
        self.callback(name,value)

    async def trace(self,name,info):
        # httpcore trace payloads can contain headers/URLs: never retain info.
        fields={'connection.connect_tcp.complete':'tcp_connected_ms',
                'connection.start_tls.complete':'tls_ready_ms',
                'http11.send_request_body.complete':'request_sent_ms',
                'http2.send_request_body.complete':'request_sent_ms'}
        if name in fields:self.data[fields[name]]=self.elapsed()

    def transport_trace(self, name, info):
        # httpcore requires a CPython coroutine object. wait_for is a stdlib
        # coroutine bridge for native-extension awaitables; None adds no timeout.
        return asyncio.wait_for(self.trace(name, info), timeout=None)

    def headers(self,response):
        self.data.update(headers_ms=self.elapsed(),http_status=response.status_code,stage='waiting_body')
        for name in ('x-request-id','request-id','x-dashscope-request-id'):
            identifier=safe_id(response.headers.get(name))
            if identifier:
                self.data['provider_request_id']=identifier;break
        self.emit('agent.request.headers','已收到模型服务响应头，等待响应内容')

    def payload(self,value):
        self.data['chunks']+=1
        if 'first_event_ms' not in self.data:
            self.data['first_event_ms']=self.elapsed()
            self.emit('agent.request.first_event','已收到首个模型响应事件')
        identifier=safe_id(value.get('id'))
        if identifier:self.data['response_id']=identifier
        usage=value.get('usage') or {}
        for key in ('prompt_tokens','completion_tokens','total_tokens'):
            count=usage.get(key)
            if isinstance(count,int) and not isinstance(count,bool) and count>=0:self.data[key]=count
        details=usage.get('completion_tokens_details') or {}
        count=details.get('reasoning_tokens')
        if isinstance(count,int) and not isinstance(count,bool) and count>=0:self.data['reasoning_tokens']=count
        choices=value.get('choices') or []
        choice=next((c for c in choices if c.get('index',0)==0),None)
        if choice is None:return None
        delta=choice.get('delta') or choice.get('message') or {}
        reasoning=delta.get('reasoning_content') or delta.get('reasoning')
        if isinstance(reasoning,str) and reasoning:
            self.data['reasoning_chars']+=len(reasoning)
            self.data['stage']='receiving_reasoning'
            if 'first_reasoning_ms' not in self.data:
                self.data['first_reasoning_ms']=self.elapsed()
                self.emit('agent.request.reasoning_started','服务正在返回推理片段，仅记录数量，不保存正文')
        content=delta.get('content')
        content_len=len(content) if isinstance(content,str) else 0
        calls=delta.get('tool_calls') or []
        args_len=sum(len(c.get('function',{}).get('arguments') or '') for c in calls)
        if (isinstance(content,str) and content.strip()) or any((c.get('function',{}).get('arguments') or '').strip() for c in calls):
            if not self.delivery_started.is_set():self.data['first_delivery_ms']=self.elapsed()
            self.delivery_started.set()
        self.data['output_chars']+=content_len
        self.data['tool_argument_chars']+=args_len
        if content_len or calls:
            self.data['stage']='receiving_output'
            if 'first_output_ms' not in self.data:
                self.data['first_output_ms']=self.elapsed()
                self.emit('agent.request.output_started','开始收到答案或工具调用参数')
        if choice.get('finish_reason'):
            reason=choice['finish_reason']
            if reason in {'stop','length','tool_calls','content_filter','function_call'}:self.data['finish_reason']=reason
        return choice

    async def heartbeat(self):
        while True:
            await asyncio.sleep(15)
            labels={'waiting_headers':'等待模型服务响应头','waiting_body':'已收到响应头，等待内容','receiving_reasoning':'正在接收推理片段','receiving_output':'正在接收答案或工具参数'}
            self.emit('agent.request.progress',f"{labels.get(self.data['stage'],'等待模型完成')}；已等待 {self.elapsed()//1000} 秒，推理字符 {self.data['reasoning_chars']}，输出字符 {self.data['output_chars']}，工具参数字符 {self.data['tool_argument_chars']}")


async def read_response(response,diagnostics):
    """Assemble public text/tool deltas only; discard reasoning immediately."""
    if 'text/event-stream' not in response.headers.get('content-type','').lower():
        diagnostics.data['response_mode']='json'
        await response.aread()
        value=response.json()
        choice=diagnostics.payload(value)
        if choice is None:raise ValueError('missing choice')
        message=choice['message']
        limit=diagnostics.data.get('max_tool_argument_chars')
        arguments=''.join(c.get('function',{}).get('arguments') or '' for c in message.get('tool_calls') or [])
        if limit and len(arguments)>limit:
            diagnostics.data.update(public_argument_stats(arguments),failure_kind='public_output_limit')
            diagnostics.emit('agent.request.output_limit','结构化工具参数超过当前阶段限制；丢弃产物')
            raise PublicOutputLimitError('public tool arguments exceed stage contract')
        return {'content':message.get('content'),'tool_calls':message.get('tool_calls') or [],'finish_reason':choice.get('finish_reason')}
    diagnostics.data['response_mode']='sse'
    parts=[];calls={};finish=None;done=False;lines=[];size=0;finished_at=None
    def consume(data):
        nonlocal finish,done,finished_at
        if data=='[DONE]':done=True;return
        value=json.loads(data)
        if 'error' in value:raise ValueError('provider stream error')
        choice=diagnostics.payload(value)
        if choice is None:return
        delta=choice.get('delta') or {}
        if finish:
            if any(delta.get(k) for k in ('content','tool_calls','reasoning_content','reasoning')):
                raise ValueError('provider sent output after finish_reason')
            return
        if isinstance(delta.get('content'),str):parts.append(delta['content'])
        for item in delta.get('tool_calls') or []:
            index=item.get('index',0)
            if not isinstance(index,int) or index<0 or index>32:raise ValueError('invalid tool index')
            call=calls.setdefault(index,{'id':'','type':'function','function':{'name':'','arguments':''}})
            if item.get('id'):
                if call['id'] and call['id'] != item['id']:
                    raise ValueError('provider changed tool id within one index')
                if call['id']:
                    diagnostics.data['repeated_tool_ids']=diagnostics.data.get('repeated_tool_ids',0)+1
                call['id']=item['id']
            function=item.get('function') or {}
            call['function']['name']+=function.get('name') or ''
            call['function']['arguments']+=function.get('arguments') or ''
            limit=diagnostics.data.get('max_tool_argument_chars')
            if limit and sum(len(c['function']['arguments']) for c in calls.values())>limit:
                diagnostics.data.update(public_argument_stats(call['function']['arguments']))
                diagnostics.data['failure_kind']='public_output_limit'
                diagnostics.emit('agent.request.output_limit','结构化工具参数超过当前阶段限制；丢弃未终止产物')
                raise PublicOutputLimitError('public tool arguments exceed stage contract')
        if choice.get('finish_reason'):
            if choice['finish_reason'] not in {'stop','tool_calls','length','content_filter','function_call'}:
                raise ValueError('unknown finish_reason')
            finish=choice['finish_reason'];finished_at=time.monotonic()
            diagnostics.data['finish_received_ms']=diagnostics.elapsed()
    iterator=response.aiter_lines().__aiter__()
    while True:
        try:
            if finished_at is not None:
                remaining=.25-(time.monotonic()-finished_at)
                if remaining<=0:
                    diagnostics.data['stream_end']='finish_reason_grace_elapsed';break
                async with asyncio.timeout(remaining):
                    line=await anext(iterator)
            else:
                line=await anext(iterator)
        except StopAsyncIteration:
            diagnostics.data['stream_end']='eof';break
        except TimeoutError:
            if not finish:raise
            diagnostics.data['stream_end']='finish_reason_grace_elapsed';break
        size+=len(line.encode('utf-8'))
        if size>16*1024*1024:raise ValueError('model stream exceeds limit')
        if not line:
            if lines:
                consume('\n'.join(lines));lines=[]
                if done:
                    diagnostics.data['stream_end']='done';break
        elif line.startswith('data:'):lines.append(line[5:].lstrip())
    if lines and not finish:consume('\n'.join(lines))
    if not finish:raise ValueError('incomplete model stream')
    if calls:
        diagnostics.data.update(public_argument_stats(''.join(c['function']['arguments'] for c in calls.values())))
    return {'content':''.join(parts) or None,'tool_calls':[calls[i] for i in sorted(calls)],'finish_reason':finish}


_delivery_budget = ContextVar('semora_delivery_budget', default=None)

@contextmanager
def delivery_budget(seconds, message):
    token = _delivery_budget.set((seconds, message))
    try:
        yield
    finally:
        _delivery_budget.reset(token)

def current_delivery_budget():
    return _delivery_budget.get()


_json_options = ContextVar('semora_json_options', default=None)

@contextmanager
def json_model_options(response_format, thinking):
    if response_format not in {'json_object', 'text'} or thinking not in {'inherit', 'omit', 'false', 'true'}:
        raise ValueError('Invalid JSON model adapter options')
    token=_json_options.set((response_format, thinking))
    try:
        yield
    finally:
        _json_options.reset(token)

def current_json_options():
    return _json_options.get()
