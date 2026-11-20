from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

import psycopg
from semora_contracts import SystemSettings


def _policy_version(settings: SystemSettings) -> str:
    payload = settings.model_dump(
        mode="json",
        exclude={"version", "policy_version", "updated_at", "updated_by"},
    )
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    return f"query-policy-v{settings.version}-{digest}"


class SettingsRepository:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn if dsn is not None else os.getenv("METADATA_DATABASE_URL")
        self._settings = SystemSettings()
        self._settings.policy_version = _policy_version(self._settings)
        if self._dsn:
            self._load()

    def _connect(self):
        return psycopg.connect(self._dsn, autocommit=True)

    def _load(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM semora_meta.system_settings WHERE id=1")
            row = cursor.fetchone()
            if row:
                self._settings = SystemSettings.model_validate(row[0])
                # Recalculate so old or manually edited payloads cannot forge a policy id.
                self._settings.policy_version = _policy_version(self._settings)
            else:
                self._persist(self._settings)

    def _persist(self, settings: SystemSettings) -> None:
        if not self._dsn:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO semora_meta.system_settings(
                    id,payload,version,policy_version,updated_at,updated_by
                ) VALUES(1,%s::jsonb,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                    payload=excluded.payload,version=excluded.version,
                    policy_version=excluded.policy_version,updated_at=excluded.updated_at,
                    updated_by=excluded.updated_by""",
                (
                    settings.model_dump_json(),
                    settings.version,
                    settings.policy_version,
                    settings.updated_at,
                    settings.updated_by,
                ),
            )

    def get(self) -> SystemSettings:
        return self._settings.model_copy(deep=True)

    def update(self, value: SystemSettings, actor: str) -> SystemSettings:
        value.version = self._settings.version + 1
        value.updated_at = datetime.now(timezone.utc)
        value.updated_by = actor
        if value.preview_rows > value.max_result_rows:
            raise ValueError("页面预览行数不能大于最大返回行数")
        value.policy_version = _policy_version(value)
        self._persist(value)
        self._settings = value.model_copy(deep=True)
        return self.get()


settings_service = SettingsRepository()
