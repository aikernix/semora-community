"""Executor-owned connection configuration. Passwords never leave this service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Literal
from urllib.parse import quote, unquote, urlsplit, urlencode

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from psycopg.conninfo import conninfo_to_dict, make_conninfo


from semora_contracts.database import DatabaseConnection as ConnectionSettings


class ConnectionStore:
    def __init__(self, directory: str | None = None):
        self.directory = Path(directory or os.getenv("EXECUTOR_CONFIG_DIR", "/data/config"))
        self.lock = threading.RLock()

    def _cipher(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_path = self.directory / "key"
        if not key_path.exists():
            if (self.directory / "connections.enc").exists():
                raise RuntimeError("连接配置加密密钥缺失，请恢复 Executor 配置卷备份")
            try:
                with key_path.open("xb") as stream:
                    os.chmod(key_path, 0o600)
                    stream.write(Fernet.generate_key())
            except FileExistsError:
                pass
        return Fernet(key_path.read_bytes())

    def _read(self):
        path = self.directory / "connections.enc"
        if not path.exists():
            return {}
        return json.loads(self._cipher().decrypt(path.read_bytes()))

    @staticmethod
    def _legacy(secret_ref: str, datasource_type: str):
        dsn = os.getenv(secret_ref, "")
        if not dsn:
            return None
        if datasource_type in {"postgresql", "vastbase"}:
            value = conninfo_to_dict(dsn)
            return dict(type=datasource_type, host=value.get("host", "localhost"),
                        port=int(value.get("port", 5432)), database=value.get("dbname", ""),
                        username=value.get("user", ""), password=value.get("password", ""),
                        _options={k: v for k, v in value.items() if k not in {"host", "port", "dbname", "user", "password"}})
        if datasource_type not in {"mysql", "goldendb"}:
            return None
        url = urlsplit(dsn)
        return dict(type=datasource_type, host=url.hostname or "", port=url.port or 3306,
                    database=unquote(url.path.lstrip("/")), username=unquote(url.username or ""),
                    password=unquote(url.password or ""))

    def get(self, datasource_id: str, secret_ref: str, datasource_type: str):
        with self.lock:
            return self._read().get(datasource_id) or self._legacy(secret_ref, datasource_type)

    def resolve(self, datasource_id: str, secret_ref: str):
        with self.lock:
            stored = self._read().get(datasource_id)
        return self.dsn(stored) if stored else os.getenv(secret_ref)

    @staticmethod
    def public(value):
        if value is None:
            return {"configured": False, "password_configured": False}
        return {**{k: v for k, v in value.items() if k not in {"password", "_options", "_validation"}},
                "configured": True, "password_configured": bool(value.get("password"))}

    def candidate(self, datasource_id: str, secret_ref: str, settings: ConnectionSettings):
        value = settings.model_dump(exclude={"password"})
        previous = self.get(datasource_id, secret_ref, settings.type)
        if previous and previous.get("_options"):
            value["_options"] = previous["_options"]
        value["password"] = settings.password.get_secret_value() or (previous or {}).get("password", "")
        if not value["password"] and previous is None:
            raise ValueError("新增数据源请填写密码")
        return value

    def save(self, datasource_id: str, value):
        with self.lock:
            cipher = self._cipher()
            values = self._read()
            values[datasource_id] = value
            fd, temporary = tempfile.mkstemp(dir=self.directory, prefix="connections-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(cipher.encrypt(json.dumps(values).encode()))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.directory / "connections.enc")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    @staticmethod
    def dsn(value):
        if value["type"] in {"postgresql", "vastbase"}:
            return make_conninfo(host=value["host"], port=value["port"], dbname=value["database"],
                                 user=value["username"], password=value["password"], **{**value.get("_options", {}), **value.get("properties", {})})
        host = value["host"]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if value["type"] not in {"mysql", "goldendb"}:
            raise ValueError("JDBC 配置不生成原生 DSN")
        scheme = "mysql"
        query = "?" + urlencode(value["properties"]) if value.get("properties") else ""
        return (f"{scheme}://{quote(value['username'], safe='')}:{quote(value['password'], safe='')}"
                f"@{host}:{value['port']}/{quote(value['database'], safe='')}{query}")


connection_store = ConnectionStore()
