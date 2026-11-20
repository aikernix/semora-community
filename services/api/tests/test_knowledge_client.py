import asyncio

import httpx

from app.knowledge import WeKnoraClient


def test_weknora_retrieval_reads_search_hits_and_returns_traceable_evidence(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from semora_agent import model as model_module
    monkeypatch.setattr(model_module, "structured_model", SimpleNamespace(complete_json=AsyncMock(return_value={"knowledge_keywords": ["余额"]})))
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.headers.get("X-API-Key")))
        if request.url.path == "/api/v1/knowledgebase/kb-1/wiki/search":
            return httpx.Response(
                200,
                json={"pages": [{"slug": "concept/loan-balance", "title": "贷款余额"}]},
            )
        if request.url.path == "/api/v1/knowledgebase/kb-1/wiki/pages/concept/loan-balance":
            return httpx.Response(
                200,
                json={
                    "id": "page-1",
                    "slug": "concept/loan-balance",
                    "title": "贷款余额",
                    "content": "贷款余额取有效贷款余额之和。",
                    "version": 3,
                    "source_refs": ["doc-1"],
                    "chunk_refs": ["chunk-1"],
                    "out_links": ["entity/loan"],
                    "in_links": [],
                },
            )
        assert request.url.path == "/api/v1/knowledgebase/kb-1/wiki/pages/entity/loan"
        return httpx.Response(
            200,
            json={
                "id": "page-2",
                "slug": "entity/loan",
                "title": "贷款",
                "content": "贷款是授信合同项下的实际放款记录。",
                "version": 2,
                "source_refs": ["doc-2"],
                "chunk_refs": ["chunk-2"],
                "out_links": [],
                "in_links": ["concept/loan-balance"],
            },
        )

    client = WeKnoraClient("https://weknora.example/api/v1", "wk-secret", httpx.MockTransport(handler))
    result = asyncio.run(client.research("贷款余额是多少", ["kb-1"]))

    assert requests == [
        ("GET", "/api/v1/knowledgebase/kb-1/wiki/search", "wk-secret"),
        ("GET", "/api/v1/knowledgebase/kb-1/wiki/pages/concept/loan-balance", "wk-secret"),
    ]
    assert result.agent_id == "semora-workbench-knowledge-retrieval"
    assert result.session_id == ""
    assert result.evidence[0].title == "贷款余额"
    assert result.evidence[0].chunk_id == "chunk-1"
    assert any(step.tool == "wiki_search" for step in result.steps)
    assert any(step.tool == "wiki_read_page" for step in result.steps)
    assert len(result.evidence) == 1
