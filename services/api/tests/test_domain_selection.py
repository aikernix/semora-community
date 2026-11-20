"""Domain selection must come from the request/session, never a demo fallback."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_sql_native import native_harness


def test_workflow_uses_the_bound_session_domain_when_omitted(monkeypatch):
    w,session,execute,*_= native_harness(monkeypatch)
    run=asyncio.run(w.start(session.id,'List items'))
    assert run.domain==session.domain=='test'
    assert run.status.value=='completed',run.error
    execute.assert_awaited_once()


def test_unbound_or_mismatched_domain_does_not_start_analysis(monkeypatch):
    w,session,execute,estimate,*_=native_harness(monkeypatch)
    with pytest.raises(ValueError,match='其他业务域'):
        asyncio.run(w.start(session.id,'List items',domain='other'))
    assert not session.run_ids
    session.domain=''
    with pytest.raises(ValueError,match='选择业务域'):
        asyncio.run(w.start(session.id,'List items'))
    assert not session.run_ids
    execute.assert_not_called();estimate.assert_not_called()


def test_http_operations_require_explicit_domain_without_accessing_ontology(monkeypatch):
    monkeypatch.delenv('METADATA_DATABASE_URL',raising=False)
    monkeypatch.setenv('AUTH_REQUIRED','false')
    from app import main
    read=AsyncMock(side_effect=AssertionError('Missing domain must fail before any ontology access'))
    monkeypatch.setattr(main.ontology_service,'get_draft',read)
    monkeypatch.setattr(main.ontology_service,'find_path',read)
    client=TestClient(main.app)
    assert client.post('/api/v1/sessions',json={'title':'Test'}).status_code==422
    assert client.post('/api/v1/ontology/publish').status_code==422
    assert client.get('/api/v1/ontology/paths',params={'source':'entity','target':'event'}).status_code==422
    assert client.post('/api/v1/knowledge/document/ontology-candidates').status_code==422
    read.assert_not_called()
