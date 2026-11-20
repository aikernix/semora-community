import asyncio

import pytest

from app.settings import SettingsRepository
from semora_contracts import SystemSettings


def test_settings_policy_is_versioned_and_hashed():
    repository = SettingsRepository(dsn=None)
    original = repository.get()
    changed = original.model_copy(deep=True)
    changed.query_timeout_seconds = 20

    saved = repository.update(changed, "security.admin")

    assert saved.version == original.version + 1
    assert saved.policy_version != original.policy_version
    assert saved.policy_version.startswith(f"query-policy-v{saved.version}-")
    assert saved.updated_by == "security.admin"


def test_settings_reject_preview_above_result_limit():
    repository = SettingsRepository(dsn=None)
    changed = repository.get()
    changed.max_result_rows = 50
    changed.preview_rows = 100

    with pytest.raises(ValueError, match="预览行数"):
        repository.update(changed, "security.admin")


def test_settings_keep_defaults_and_accept_values_above_former_limits(monkeypatch):
    monkeypatch.setenv("QUERY_TIMEOUT_MS", "12000")
    monkeypatch.setenv("MAX_RESULT_ROWS", "80")
    monkeypatch.setenv("MAX_RESULT_BYTES", str(2 * 1024 * 1024))
    repository = SettingsRepository(dsn="")
    saved = repository.get()
    assert (saved.query_timeout_seconds, saved.max_result_rows, saved.max_result_mb) == (30, 1000, 10)
    assert SystemSettings.model_validate({"query_timeout_seconds": 9}).explain_timeout_seconds == 30
    updated = SystemSettings(query_timeout_seconds=120, explain_timeout_seconds=90,
                             max_result_rows=2500, max_result_mb=64)
    saved = repository.update(updated, "security.admin")
    assert (saved.query_timeout_seconds, saved.explain_timeout_seconds,
            saved.max_result_rows, saved.max_result_mb) == (120, 90, 2500, 64)


def test_query_and_explain_send_policy_values_with_transport_grace(monkeypatch):
    import json
    import httpx
    from app.executor_client import ExecutorClient

    received = []

    def reply(request):
        received.append((json.loads(request.content), request.extensions["timeout"]["read"]))
        if request.url.path.endswith("/estimate"):
            return httpx.Response(200, json={"estimated_rows": 1})
        return httpx.Response(200, json={"columns": ["n"], "rows": [[1]], "row_count": 1, "duration_ms": 1})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(reply), **kwargs))
    client = ExecutorClient()
    monkeypatch.setattr(client, "datasource_payload", lambda source: {"datasource_id": source})
    async def run():
        await client.execute("SELECT 1", {}, [], {
            "query_timeout_seconds": 7, "max_result_rows": 4, "max_result_mb": 2, "data_source_id": "demo",
        })
        await client.estimate("SELECT 1", {}, "demo", 90)

    asyncio.run(run())
    assert [(body["timeout_ms"], timeout) for body, timeout in received] == [(7000, 12), (90000, 95)]
    assert received[0][0]["max_rows"] == 4
    assert received[0][0]["max_bytes"] == 2 * 1024 * 1024
