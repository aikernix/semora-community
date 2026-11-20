from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

import psycopg
from pydantic import BaseModel, SecretStr
from semora_agent.model import StructuredModel, structured_model


class ModelConfigInput(BaseModel):
    base_url: str
    model: str
    api_key: SecretStr = SecretStr('')


class ModelSettings:
    def __init__(self):
        self.dsn = os.getenv('METADATA_DATABASE_URL')
        if self.dsn:
            with psycopg.connect(self.dsn) as connection:
                row=connection.execute('SELECT payload FROM semora_meta.model_settings WHERE id=1').fetchone()
            if row:
                self.apply(row[0])
        # The API never silently substitutes deterministic analysis for an LLM.
        if self.dsn or os.getenv('LLM_MODE') == 'required':
            structured_model.mode = 'required'

    @staticmethod
    def apply(value):
        structured_model.base_url=value['base_url'].rstrip('/')
        structured_model.api_key=value['api_key']
        structured_model.model=value['model']
        structured_model.mode='required'

    def public(self):
        return {'base_url':structured_model.base_url,'model':structured_model.model,
                'api_key_configured':bool(structured_model.api_key),'configured':structured_model.configured}

    def candidate(self, value: ModelConfigInput):
        url=value.base_url.strip().rstrip('/')
        parsed=urlsplit(url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('模型地址必须是有效的 HTTP(S) API 地址，不能包含凭据、查询参数或片段')
        if not value.model.strip():
            raise ValueError('请填写模型名称')
        key=value.api_key.get_secret_value().strip()
        if not key and url!=structured_model.base_url:
            raise ValueError('更换模型服务地址时，请重新输入 API Key')
        key=key or structured_model.api_key
        if not key:
            raise ValueError('请填写 API Key')
        candidate=StructuredModel()
        candidate.base_url=url
        candidate.api_key=key
        candidate.model=value.model.strip()
        candidate.mode='required'
        return candidate

    def save(self, value: ModelConfigInput):
        candidate=self.candidate(value)
        payload={'base_url':candidate.base_url,'model':candidate.model,'api_key':candidate.api_key}
        if self.dsn:
            with psycopg.connect(self.dsn) as connection:
                connection.execute('''INSERT INTO semora_meta.model_settings(id,payload) VALUES(1,%s::jsonb)
                    ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=now()''',(json.dumps(payload),))
        self.apply(payload)
        return self.public()


model_settings=ModelSettings()
