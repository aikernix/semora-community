import httpx
import asyncio
from fastapi.testclient import TestClient

from app.ontology import OntologyCoreClient


def test_binding_client_only_sends_ids(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == "PATCH"
        assert request.url.path.endswith("/drafts/example/knowledge-bases")
        assert request.content == b'{"knowledgeBaseIds":["kb-one"]}'
        return httpx.Response(200, json={"domain": "example", "knowledgeBaseIds": ["kb-one"]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    result = asyncio.run(OntologyCoreClient(base_url="http://ontology").bind_knowledge_bases("example", ["kb-one"]))
    assert result["knowledgeBaseIds"] == ["kb-one"]
    assert len(requests) == 1


def test_binding_route_checks_access_and_never_loads_or_saves_full_ontology(monkeypatch):
    from app import main
    calls = []
    class Knowledge:
        async def list_knowledge_bases(self): return [{"id": "kb-one"}]
    class Ontology:
        async def bind_knowledge_bases(self, domain, ids):
            calls.append((domain, ids))
            return {"domain": domain, "knowledgeBaseIds": ids}
    monkeypatch.setattr(main, "ontology_service", Ontology())
    monkeypatch.setattr(main, "knowledge_service", Knowledge())
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    client = TestClient(main.app)
    path = "/api/v1/ontology/drafts/example/knowledge-bases"
    assert client.patch(path, json={"knowledgeBaseIds": ["kb-one", "kb-one"]}).status_code == 200
    assert calls == [("example", ["kb-one"])]
    assert client.patch(path, json={"knowledgeBaseIds": ["not-accessible"]}).status_code == 422
    assert len(calls) == 1
    assert client.patch(path, json={"knowledgeBaseIds": [], "objects": []}).status_code == 422
    assert client.patch(path, json={}).status_code == 422
    assert client.patch(path, json={"knowledgeBaseIds": []}).status_code == 200
    assert calls[-1] == ("example", [])
