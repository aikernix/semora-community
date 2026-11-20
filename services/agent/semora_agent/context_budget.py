"""Lossless request compaction; durable messages are never modified."""
import copy
import json
import os
import re

from .model import ModelError


class ContextBudgetError(ModelError):
    def __init__(self,stats):
        self.stats=stats
        super().__init__(f'上下文在无损精简后仍有 {stats["request_bytes"]} 字节，超过 {stats["limit_bytes"]} 字节预算。已保留完整需求与证据，未截断关键事实；任务需重新组织上下文，未继续调用模型。')


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8'))


def prepare_context(messages, tools, limit_bytes=None):
    original_size = encoded_size({'messages': messages, 'tools': tools})
    result = copy.deepcopy(messages)
    observations = {}
    repeated = 0
    for message in result:
        content = message.get('content')
        if not isinstance(content, str):
            continue
        try:
            json.loads(content)
        except (ValueError, TypeError):
            continue
        # Preserve JSON types, all fields and array order. Never summarize facts.
        compact = re.sub(r'("(?:\\.|[^"\\])*")|\s+', lambda match: match.group(1) or '', content)
        message['content'] = compact
        if message.get('role') != 'tool' or not message.get('tool_call_id'):
            continue
        if compact in observations:
            reference = json.dumps({'identical_result_reference': observations[compact],
                                    'instruction': '本次工具返回内容与此前该tool_call_id的完整结果完全相同，请复用其全部内容。'}, ensure_ascii=False, separators=(',', ':'))
            if len(reference.encode('utf-8')) < len(compact.encode('utf-8')):
                message['content'] = reference
                repeated += 1
        else:
            observations[compact] = message['tool_call_id']
    size = encoded_size({'messages': result, 'tools': tools})
    # A byte limit is deterministic across compatible providers. Do not label
    # it as an exact token count: each model has its own tokenizer.
    limit = int(os.getenv('AGENT_CONTEXT_MAX_BYTES', '256000'))
    if limit_bytes is not None:limit=min(limit,int(limit_bytes))
    if limit <= 0:
        raise ModelError('AGENT_CONTEXT_MAX_BYTES 必须大于0')
    stats = {'original_bytes': original_size, 'request_bytes': size,
             'messages_bytes': encoded_size(result), 'tool_schema_bytes': encoded_size(tools),
             'limit_bytes': limit, 'reused_observations': repeated}
    if size > limit:
        raise ContextBudgetError(stats)
    return result, stats
