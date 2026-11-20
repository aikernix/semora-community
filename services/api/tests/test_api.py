from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health_and_catalog_endpoints():
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/api/v1/datasources").json()[0]["access"] == "read_only"
    settings = client.get("/api/v1/settings").json()
    assert "approval_mode" not in settings and "delivery_mode" not in settings
    assert settings["policy_version"].startswith("query-policy-v")


def test_query_payload_rejects_retired_policy_fields():
    response = client.post(
        "/api/v1/sessions/demo-session/messages",
        json={"question": "贷款余额", "approval_mode": "disabled"},
    )
    assert response.status_code == 422


def test_datasource_registration_stores_secret_reference_not_dsn():
    response = client.post(
        "/api/v1/datasources",
        json={
            "name": "测试只读库",
            "type": "postgresql",
            "secret_ref": "DATASOURCE_TEST_BANK_URL",
        },
    )

    assert response.status_code == 201
    assert response.json()["secret_ref"] == "DATASOURCE_TEST_BANK_URL"
    assert "password" not in response.text.lower()


def test_datasource_registration_rejects_raw_connection_string():
    response = client.post(
        "/api/v1/datasources",
        json={
            "name": "不安全配置",
            "type": "postgresql",
            "secret_ref": "postgresql://user:password@db/bank",
        },
    )

    assert response.status_code == 422

