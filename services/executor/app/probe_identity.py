"""Opaque connection/adapter identities; credentials never leave Executor."""
import hashlib
import json
from .connection_settings import connection_store


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def attach_identity(result, body):
    from . import adapter_registry, jdbc_adapter
    cfg = connection_store.get(body.datasource_id, body.secret_ref, body.type) or {}
    config = {k: v for k, v in cfg.items() if k not in {"password", "_validation", "driver_id"}}
    if adapter_registry.is_jdbc(body.type):
        driver, dialect, strategy = jdbc_adapter.configuration(cfg)
        adapter = {k: v for k, v in driver.items() if k not in {"classpath", "name", "enabled", "id", "files"}}
        adapter["jar_hashes"] = sorted(file["sha256"] for file in driver.get("files", []) if file.get("sha256"))
        adapter.update(dialect=dialect, readonly_strategy=strategy,
                       session_properties_fingerprint=fingerprint(cfg.get("properties", {})))
    else:
        dialect = adapter_registry.dialect(body)
        adapter = {"type": body.type, "driver": result.get("driver"), "driver_version": result.get("driver_version")}
    result.update(dialect=dialect, adapter_fingerprint=fingerprint(adapter),
                  connection_fingerprint=fingerprint({"configuration": config, "adapter": adapter}))
    return result
