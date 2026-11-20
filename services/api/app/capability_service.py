"""Bounded source validation with shared baselines and actual instance checks."""
import asyncio
from datetime import datetime, timezone
import logging
import weakref

from fastapi import HTTPException
from semora_contracts.sql_probes import probe_catalog, SMOKE_PROBES
from .capability_cache import profile_descriptor, profile_key

LOG = logging.getLogger(__name__)
TASKS = {}
LOCKS = weakref.WeakKeyDictionary()
SLOTS = weakref.WeakKeyDictionary()


async def validate_cached(item, repository, request, force=False):
    body = {"datasource_id": item.id, "type": item.type, "secret_ref": item.secret_ref}
    fresh = await request("POST", "datasources/test", json=body)
    if not fresh.get("read_only"):
        raise HTTPException(409, "请先验证数据源只读执行能力")
    repository.record_test(item.id, fresh)
    descriptor = profile_descriptor(item.type, fresh, fresh.get("capabilities"))
    key = profile_key(descriptor) if descriptor else item.id
    lock = LOCKS.setdefault(repository.capability_cache, {}).setdefault(key, asyncio.Lock())
    async with lock:
        return await _validate_ready(item, repository, request, fresh, force)


async def _validate_ready(item, repository, request, fresh, force):
    body = {"datasource_id": item.id, "type": item.type, "secret_ref": item.secret_ref}
    fingerprint = fresh.get("connection_fingerprint")
    descriptor = profile_descriptor(item.type, fresh, fresh.get("capabilities"))
    dialect = descriptor["dialect"] if descriptor else fresh.get("dialect") or {"postgresql": "postgres", "sqlserver": "tsql"}.get(item.type, item.type)
    catalog = probe_catalog(dialect)
    cached = None
    try:
        if not force:
            cached = repository.capability_cache.resolve(item.type, fresh, fresh.get("capabilities"))
    except Exception as exc:
        LOG.warning("Capability cache read unavailable: %s", type(exc).__name__)
    exact = cached.get("instance_probes", {}) if cached else {}
    shared = cached.get("shared_probes", {}) if cached else {}
    reusable = {key for key, value in shared.items() if value.get("status") in {"verified", "unsupported"}} | set(exact)
    if exact:
        keys = [key for key in catalog if key not in reusable]
        mode = "instance"
    elif cached:
        keys = [key for key in catalog if key in SMOKE_PROBES or shared.get(key, {}).get("status") not in {"verified", "unsupported"}]
        mode = "shared"
    else:
        keys, mode = list(catalog), "full"
    result = await request("POST", "datasources/validate-query", json={**body, "probe_keys": keys})
    if fingerprint and (result.get("connection_fingerprint") != fingerprint or repository.get(item.id).scan_result.get("connection", {}).get("connection_fingerprint") != fingerprint):
        raise HTTPException(409, "连接配置已变化，本次探测结果已丢弃，请重新验证")
    actual = result.get("probes", {})
    if cached:
        cached = {**cached, "mode": mode, "executed": len(actual), "reused": len(reusable - set(actual))}
        item.scan_result["capability_cache"] = cached
    else:
        item.scan_result.pop("capability_cache", None)
    inherited = {k: {**v, "expires_at": cached["expires_at"]} for k, v in exact.items()} if cached else {}
    result = {**result, "probes": {**inherited, **actual}, "probe_cache": {
        "mode": mode, "executed": len(actual), "reused": len(reusable - set(actual)),
        "basic_query_verified": True,
    }}
    repository.record_test(item.id, result)
    try:
        repository.capability_cache.publish(item.type, result, result.get("capabilities"), item.id, actual)
    except Exception as exc:
        LOG.warning("Capability cache write unavailable: %s", type(exc).__name__)
    return result


def schedule_validation(item, repository, request):
    connection = item.scan_result.get("connection", {})
    if item.id in TASKS or profile_descriptor(item.type, connection, item.scan_result.get("capabilities")) is None:
        return
    slots = SLOTS.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(2))
    async def work():
        try:
            async with slots:
                return await validate_cached(item, repository, request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Background capability validation unavailable: %s", type(exc).__name__)
        finally:
            TASKS.pop(item.id, None)
    TASKS[item.id] = asyncio.create_task(work())
