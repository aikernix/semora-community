"""The knowledge test page and workbench share one keyword retrieval service."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.knowledge import KnowledgeCoreError, WeKnoraClient
from app.knowledge_retrieval import retrieve_knowledge
from semora_agent import model as model_module


def setup_retrieval(monkeypatch, keywords=None):
    model = SimpleNamespace(complete_json=AsyncMock(return_value={
        'knowledge_keywords': keywords if keywords is not None else ['对象', '状态']}))
    monkeypatch.setattr(model_module, 'structured_model', model)
    requests = []
    def handler(request):
        requests.append((request.url.path, request.url.params.get('q')))
        if request.url.path.endswith('/wiki/search'):
            return httpx.Response(200, json={'data': {'pages': [
                {'slug': 'concept/objects', 'title': '对象定义'},
                {'slug': 'concept/status', 'title': '状态定义'}]}})
        slug = request.url.path.split('/wiki/pages/')[-1]
        assert slug in ['concept/objects', 'concept/status']
        return httpx.Response(200, json={'data': {
            'slug': slug, 'title': slug, 'content': '对象按唯一编号统计。状态由记录的生效标记确定。',
            'source_refs': ['doc-1'], 'out_links': ['related/object']}})
    client = WeKnoraClient('https://weknora.example/api/v1', 'test-key', httpx.MockTransport(handler))
    return client, model, requests


def test_retrieval_test_and_workbench_use_identical_searches_and_evidence(monkeypatch):
    client, model, requests = setup_retrieval(monkeypatch)
    shared = asyncio.run(retrieve_knowledge(model, '统计对象', ['kb-a'], client=client))
    expected_requests = requests[:]
    requests.clear()
    test_result = asyncio.run(client.research('统计对象', ['kb-a']))
    assert requests == expected_requests
    assert [q for path, q in requests if path.endswith('/search')] == ['对象', '状态']
    assert [(e.locator, e.summary) for e in test_result.evidence] == [(e.locator, e.summary) for e in shared.evidence]
    assert model.complete_json.await_count == 2  # one keyword extraction per request
    assert all('action_contract' not in json.loads(c.args[1]) for c in model.complete_json.await_args_list)
    assert test_result.agent_id == 'semora-workbench-knowledge-retrieval'
    assert not any('related/object' in path for path, _ in requests)
    assert {'kb_id': 'kb-a', 'kind': 'page', 'ref': 'related/object'} in shared.discoveries
    assert {'kb_id': 'kb-a', 'kind': 'source', 'ref': 'doc-1'} in shared.discoveries


def test_keywords_are_unique_bounded_and_literal(monkeypatch):
    client, model, requests = setup_retrieval(monkeypatch, ['alpha+', 'alpha+', 'beta', 'gamma', 'delta', 'epsilon', 'extra'])
    asyncio.run(client.research('统计对象', ['kb-a']))
    terms = [q for path, q in requests if path.endswith('/search')]
    assert terms == [r'alpha\+', 'beta', 'gamma', 'delta', 'epsilon']
    assert model.complete_json.await_count == 1


def test_keyword_model_failure_falls_back_without_a_react_loop(monkeypatch):
    client, model, requests = setup_retrieval(monkeypatch)
    model.complete_json.side_effect = RuntimeError('model unavailable')
    result = asyncio.run(client.research('对象 状态', ['kb-a']))
    assert [q for path, q in requests if path.endswith('/search')] == ['对象', '状态']
    assert len(result.evidence) == 2
    assert model.complete_json.await_count == 1
    assert any(s.kind == 'fallback' for s in result.steps)


def test_reading_is_capped_and_never_guesses_identifiers(monkeypatch):
    client, model, requests = setup_retrieval(monkeypatch)
    client._wiki_search = AsyncMock(side_effect=[
        [{'slug': f'p{i}'} for i in range(5)], [{'slug': f'p{i}'} for i in range(5, 10)]])
    client._wiki_page = AsyncMock(side_effect=lambda kb, slug: {'slug': slug, 'content': '对象定义'})
    result = asyncio.run(client.research('统计对象', ['kb-a']))
    assert client._wiki_page.await_count == 3
    assert [call.args for call in client._wiki_page.await_args_list] == [('kb-a', 'p0'), ('kb-a', 'p1'), ('kb-a', 'p5')]
    assert len(result.evidence) == 3


def test_no_hits_and_service_errors_return_inspectable_status(monkeypatch):
    client, _, _ = setup_retrieval(monkeypatch)
    client._wiki_search = AsyncMock(side_effect=[RuntimeError('offline'), []])
    result = asyncio.run(client.research('统计对象', ['kb-a']))
    assert result.evidence == []
    assert '未取得可用页面' in result.answer
    assert any(s.status == 'unavailable' for s in result.steps)
    assert any('找到 0 个页面' in s.summary for s in result.steps)


def test_empty_binding_is_not_replaced_with_another_library(monkeypatch):
    client, model, requests = setup_retrieval(monkeypatch)
    with pytest.raises(KnowledgeCoreError, match='选择'):
        asyncio.run(client.research('统计对象', []))
    assert not requests
    model.complete_json.assert_not_awaited()


def test_workbench_delegates_to_the_shared_service(monkeypatch):
    from helpers import harness
    from app import prepared_context
    from semora_contracts import QueryRun
    w, session, *_ = harness(monkeypatch)
    run = QueryRun(session_id=session.id, domain='test', question='统计对象')
    _, ontology = asyncio.run(w.snapshot_loader('test'))
    ontology['knowledgeBaseIds'] = ['kb-a']
    client, model, _ = setup_retrieval(monkeypatch)
    w.model = model
    monkeypatch.setattr(prepared_context, 'knowledge_service', client)
    shared = AsyncMock(wraps=retrieve_knowledge)
    monkeypatch.setattr(prepared_context, 'retrieve_knowledge', shared)
    prepared = asyncio.run(prepared_context.prepare_context(w, run, run.question, ontology, {}))
    shared.assert_awaited_once()
    assert prepared['packet']['knowledge_keywords'] == ['对象', '状态']
    assert len(run.evidence) == 2 and run.agent_memory['knowledge_discovery']
