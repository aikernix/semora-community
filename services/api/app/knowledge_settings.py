from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

import psycopg
from pydantic import BaseModel, ConfigDict, SecretStr

from .knowledge import WeKnoraClient, knowledge_service


class KnowledgeConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    api_key: SecretStr = SecretStr("")


class KnowledgeSettings:
    def __init__(self, client=None, dsn=None):
        self.client = client or knowledge_service
        self.dsn = dsn if dsn is not None else os.getenv("METADATA_DATABASE_URL")
        if self.dsn:
            with psycopg.connect(self.dsn) as connection:
                row = connection.execute("SELECT payload FROM semora_meta.integrations WHERE name='weknora'").fetchone()
            if row:
                self._apply(row[0])

    def _apply(self, value):
        self.client.base_url = value["base_url"]
        self.client.api_key = value["api_key"]

    def public(self):
        return {"base_url": self.client.base_url, "api_key_configured": bool(self.client.api_key)}

    def candidate(self, value: KnowledgeConfigInput):
        url = value.base_url.strip().rstrip("/")
        parsed = urlsplit(url)
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("WeKnora 服务端口无效") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("请填写有效的 WeKnora HTTP(S) 服务地址")
        if not parsed.path:
            url += "/api/v1"
        elif not parsed.path.endswith("/api/v1"):
            raise ValueError("请填写服务根地址或以 /api/v1 结尾的 API 地址")
        key = value.api_key.get_secret_value().strip()
        if not key and url != self.client.base_url:
            raise ValueError("更换 WeKnora 实例时，请重新填写 API Key")
        key = key or self.client.api_key
        if not key:
            raise ValueError("请填写 API Key")
        return WeKnoraClient(base_url=url, api_key=key)

    def save(self, value: KnowledgeConfigInput):
        candidate = self.candidate(value)
        payload = {"base_url": candidate.base_url, "api_key": candidate.api_key}
        if self.dsn:
            with psycopg.connect(self.dsn) as connection:
                connection.execute("""INSERT INTO semora_meta.integrations(name,payload)
                    VALUES('weknora',%s::jsonb) ON CONFLICT(name) DO UPDATE
                    SET payload=excluded.payload,updated_at=now()""", (json.dumps(payload),))
        self._apply(payload)
        return self.public()


knowledge_settings = KnowledgeSettings()
