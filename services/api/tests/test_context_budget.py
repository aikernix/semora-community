import copy
import json

import pytest

from semora_agent.context_budget import prepare_context
from semora_agent.model import ModelError


def test_repeated_evidence_is_referenced_without_changing_history_or_tool_pairs():
    evidence = json.dumps({'definition': '不包含已结清记录，金额单位为元。' * 300})
    messages = [{'role': 'system', 'content': '不得绕过机构权限'},
                {'role': 'user', 'content': '原来查全年，现在只查九月，机构限定1。'}]
    for i in range(10):
        messages += [{'role': 'assistant', 'content': '', 'tool_calls': [{'id': str(i), 'type': 'function', 'function': {'name': 'read_wiki', 'arguments': '{}'}}]},
                     {'role': 'tool', 'tool_call_id': str(i), 'content': evidence}]
    before = copy.deepcopy(messages)
    compact, stats = prepare_context(messages, [])
    assert messages == before
    assert compact[:2] == messages[:2]
    assert len(compact) == len(messages)
    assert json.loads(compact[3]['content']) == json.loads(evidence)
    assert json.loads(compact[-1]['content'])['identical_result_reference'] == '0'
    assert stats['reused_observations'] == 9
    assert stats['request_bytes'] < stats['original_bytes'] / 4


def test_precision_strings_and_conflicting_facts_are_preserved():
    content = '{ "amount": 1234567890.1234567890123456789, "unit": "万 元", "escaped": "a\\\" b" }'
    messages = [{'role': 'tool', 'tool_call_id': 'a', 'content': content},
                {'role': 'tool', 'tool_call_id': 'b', 'content': '{"amount":0,"unit":"元"}'}]
    compact, stats = prepare_context(messages, [])
    assert '1234567890.1234567890123456789' in compact[0]['content']
    assert json.loads(compact[0]['content'])['unit'] == '万 元'
    assert json.loads(compact[0]['content'])['escaped'] == 'a" b'
    assert stats['reused_observations'] == 0


def test_budget_includes_tool_schema_and_never_truncates_facts(monkeypatch):
    monkeypatch.setenv('AGENT_CONTEXT_MAX_BYTES', '1000')
    messages = [{'role': 'user', 'content': '必须保留的用户口径'}]
    with pytest.raises(ModelError, match='未截断关键事实'):
        prepare_context(messages, [{'description': 'schema' * 300}])
    assert messages[0]['content'] == '必须保留的用户口径'
