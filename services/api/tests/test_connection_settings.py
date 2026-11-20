from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.knowledge import WeKnoraClient
from app.knowledge_settings import KnowledgeConfigInput, KnowledgeSettings
from services.executor.app import main as executor
from services.executor.app.connection_settings import ConnectionSettings, ConnectionStore


def settings(password="a@b:/#? 空格", **overrides):
    return ConnectionSettings(type="mysql", host="db.example", port=3306,
                              database="report db", username="reader@local", password=password, **overrides)


def test_executor_encrypts_persists_and_masks_credentials(tmp_path):
    store = ConnectionStore(str(tmp_path))
    value = store.candidate("one", "DATASOURCE_ONE_URL", settings())
    store.save("one", value)
    assert value["password"].encode() not in (tmp_path / "connections.enc").read_bytes()
    restored = ConnectionStore(str(tmp_path))
    assert restored.get("one", "DATASOURCE_ONE_URL", "mysql") == value
    assert "password" not in restored.public(value)
    assert restored.public(value)["password_configured"]
    assert restored.get("two", "DATASOURCE_TWO_URL", "mysql") is None
    url = urlsplit(restored.resolve("one", "DATASOURCE_ONE_URL"))
    assert unquote(url.password) == value["password"]
    assert unquote(url.username) == "reader@local"
    assert unquote(url.path[1:]) == "report db"


def test_legacy_credentials_are_readable_without_exposing_password_and_blank_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DATASOURCE_LEGACY_URL", "mysql://old:p%40ss@host:3307/db")
    store = ConnectionStore(str(tmp_path))
    old = store.get("old", "DATASOURCE_LEGACY_URL", "mysql")
    assert store.public(old)["port"] == 3307
    assert "password" not in store.public(old)
    candidate = store.candidate("old", "DATASOURCE_LEGACY_URL", settings(password=""))
    assert candidate["password"] == "p@ss"
    store.save("old", candidate)
    monkeypatch.setenv("DATASOURCE_LEGACY_URL", "mysql://old:changed@host/db")
    assert store.get("old", "DATASOURCE_LEGACY_URL", "mysql")["password"] == "p@ss"


def test_legacy_postgres_dsn_options_remain_unchanged_before_edit(tmp_path, monkeypatch):
    dsn = "postgresql://reader:pass@localhost:5432/db?sslmode=require&application_name=report"
    monkeypatch.setenv("DATASOURCE_PG_URL", dsn)
    store = ConnectionStore(str(tmp_path))
    assert store.resolve("pg", "DATASOURCE_PG_URL") == dsn
    assert store.public(store.get("pg", "DATASOURCE_PG_URL", "postgresql"))["database"] == "db"
    candidate = store.candidate("pg", "DATASOURCE_PG_URL", ConnectionSettings(
        type="postgresql", host="localhost", port=5432, database="db", username="reader", password=""))
    from psycopg.conninfo import conninfo_to_dict
    assert conninfo_to_dict(store.dsn(candidate))["sslmode"] == "require"
    assert "_options" not in store.public(candidate)


def test_new_connection_requires_password(tmp_path):
    with pytest.raises(ValueError, match="密码"):
        ConnectionStore(str(tmp_path)).candidate("new", "DATASOURCE_NEW_URL", settings(password=""))


def test_internal_configuration_requires_token_and_preview_does_not_save(tmp_path, monkeypatch):
    store = ConnectionStore(str(tmp_path))
    monkeypatch.setattr(executor, "connection_store", store)
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "true")
    monkeypatch.setenv("INTERNAL_TOKEN", "test-only-token")
    monkeypatch.setattr(executor.mysql_adapter, "inspect", lambda dsn: {"read_only": True})
    client = TestClient(executor.app)
    payload = {"datasource_id": "source", "type": "mysql", "secret_ref": "DATASOURCE_SOURCE_URL",
               "connection": {**settings().model_dump(exclude={"password"}), "password": "test-password"}}
    endpoint = "/internal/v1/datasources/configuration"
    assert client.put(endpoint, json=payload).status_code == 401
    headers = {"X-Semora-Internal-Token": "test-only-token"}
    response = client.post("/internal/v1/datasources/test-configuration", json=payload, headers=headers)
    assert response.status_code == 200
    assert not (tmp_path / "connections.enc").exists()
    response = client.put(endpoint, json=payload, headers=headers)
    assert response.status_code == 200
    assert "test-password" not in response.text
    assert "password" not in response.json()
    payload["connection"]["password"] = ""
    assert client.put(endpoint, json=payload, headers=headers).status_code == 200
    assert store.get("source", "DATASOURCE_SOURCE_URL", "mysql")["password"] == "test-password"


def test_knowledge_config_normalizes_url_keeps_key_and_rejects_cross_instance_key_reuse():
    client = WeKnoraClient(base_url="http://old.example/api/v1", api_key="private-key")
    config = KnowledgeSettings(client=client, dsn="")
    assert "api_key" not in config.public()
    assert config.candidate(KnowledgeConfigInput(base_url="http://old.example/")).api_key == "private-key"
    with pytest.raises(ValueError, match="重新填写"):
        config.save(KnowledgeConfigInput(base_url="http://new.example"))
    assert client.base_url == "http://old.example/api/v1"
    result = config.save(KnowledgeConfigInput(base_url="http://new.example", api_key="new-key"))
    assert result == {"base_url": "http://new.example/api/v1", "api_key_configured": True}
    assert client.base_url == "http://new.example/api/v1"
    assert client.api_key == "new-key"


@pytest.mark.parametrize("url", ["file:///tmp", "https://user:pass@host", "http://host?key=secret", "http://host/platform/knowledge-bases"])
def test_knowledge_rejects_invalid_service_addresses(url):
    config = KnowledgeSettings(client=WeKnoraClient(base_url="http://old/api/v1", api_key="key"), dsn="")
    with pytest.raises(ValueError):
        config.candidate(KnowledgeConfigInput(base_url=url, api_key="key"))


def test_connection_preview_role_policy():
    from app.auth import required_roles
    assert required_roles("POST", "/api/v1/datasource-connection-test") == frozenset({"data_steward", "admin"})


def test_create_requires_current_test_receipt_and_connection_is_immutable(monkeypatch):
    from app import main
    from app.datasources import DataSourceRepository, DataSourceService

    class FakeExecutor:
        def __init__(self): self.values = {}
        async def configuration(self, item, connection=None, *, test=False):
            if test: return {"database": connection["database"], "read_only": True}
            if connection is not None: self.values[item.id] = connection
            return {k: v for k, v in self.values.get(item.id, {}).items() if k != "password"}

    service = DataSourceService(repository=DataSourceRepository(dsn=""), executor=FakeExecutor())
    monkeypatch.setattr(main, "datasource_service", service)
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    client = TestClient(main.app)
    body = {"name": "Reporting", "type": "mysql", "connection": {
        **settings().model_dump(exclude={"password"}), "password": "only-executor"}}
    assert client.post("/api/v1/datasources", json=body).status_code == 422
    preview = client.post("/api/v1/datasource-connection-test", json=body)
    assert preview.status_code == 200
    body["test_receipt"] = preview.json()["test_receipt"]
    response = client.post("/api/v1/datasources", json=body)
    assert response.status_code == 201
    item = response.json()
    assert "only-executor" not in response.text
    record = service.repository.get(item["id"])
    assert "only-executor" not in repr(record)
    body["connection"]["host"] = "new.example"
    body["connection"]["password"] = ""
    assert client.post("/api/v1/datasources", json=body).status_code == 422
    response = client.put(f"/api/v1/datasources/{item['id']}", json=body)
    assert response.status_code == 409
    assert client.patch(
        f"/api/v1/datasources/{item['id']}", json={"name": "Renamed"}
    ).json()["name"] == "Renamed"
    before = len(service.repository.list())
    response = client.post("/api/v1/datasource-connection-test", json=body)
    assert response.status_code == 200
    assert len(service.repository.list()) == before


def test_knowledge_configuration_persists_and_reloads(monkeypatch):
    import app.knowledge_settings as module
    saved = {}
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, parameters=None):
            if sql.startswith("INSERT"):
                import json
                saved.update(json.loads(parameters[0]))
            return self
        def fetchone(self): return (saved.copy(),) if saved else None
    monkeypatch.setattr(module.psycopg, "connect", lambda dsn: Connection())
    first = KnowledgeSettings(client=WeKnoraClient(base_url="http://old/api/v1", api_key="old-key"), dsn="metadata")
    first.save(KnowledgeConfigInput(base_url="http://new", api_key="saved-key"))
    restarted = KnowledgeSettings(client=WeKnoraClient(base_url="http://env/api/v1", api_key="env-key"), dsn="metadata")
    assert restarted.client.base_url == "http://new/api/v1"
    assert restarted.client.api_key == "saved-key"
