"""Shared version/driver baselines; instance evidence never becomes another account's denial."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import threading

import psycopg
from semora_contracts.sql_probes import PROBE_CATALOG_VERSION, SMOKE_PROBES
from semora_contracts.database import normalize_sql_probe


def profile_descriptor(kind, connection, capabilities=None):
    capabilities = capabilities or {}
    values = {"product": connection.get("product"), "version": connection.get("version"),
              "driver": connection.get("driver"), "driver_version": connection.get("driver_version")}
    if any(not isinstance(v, str) or not v.strip() or v.lower() == "unknown" for v in values.values()):
        return None
    return {**values, "type": kind, "dialect": capabilities.get("dialect") or connection.get("dialect") or {"postgresql": "postgres", "sqlserver": "tsql"}.get(kind, kind),
            "compatibility_mode": connection.get("compatibility_mode", ""),
            "adapter_fingerprint": connection.get("adapter_fingerprint", ""), "probe_catalog": PROBE_CATALOG_VERSION}


def profile_key(descriptor):
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class CapabilityCache:
    def __init__(self, dsn=None, clock=None):
        self.dsn = os.getenv("SQL_CAPABILITY_CACHE_DATABASE_URL") or dsn
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.values = {}
        self.lock = threading.RLock()

    def publish(self, kind, connection, capabilities, source_id, probes):
        descriptor = profile_descriptor(kind, connection, capabilities)
        if descriptor is None:
            return
        dialect = descriptor["dialect"]
        evidence = {k: normalize_sql_probe(v, dialect) for k, v in probes.items()
                    if isinstance(v, dict) and v.get("method") not in {"shared_baseline", "instance_cache"}}
        evidence = {k: v for k, v in evidence.items() if v["status"] in {"verified", "unsupported"}}
        if not evidence:
            return
        key = profile_key(descriptor)
        def update(value):
            donors = value.setdefault("donors", {})
            previous = donors.get(source_id, {})
            same_instance = bool(connection.get("connection_fingerprint")) and previous.get("instance_key") == connection["connection_fingerprint"]
            fresh = {k: {**deepcopy(v), "observed_at": self.clock().isoformat()} for k, v in evidence.items()}
            donors[source_id] = {"instance_key": connection.get("connection_fingerprint", ""),
                "recorded_at": self.clock().isoformat(), "probes": {**(previous.get("probes", {}) if same_instance else {}), **fresh}}
            probes = donors[source_id]["probes"]
            recent = sorted(((k, v) for k, v in probes.items() if k.startswith("observed:")), key=lambda pair: pair[1].get("observed_at", ""))[-64:]
            donors[source_id]["probes"] = {**{k: v for k, v in probes.items() if not k.startswith("observed:")}, **dict(recent)}
            return {"donors": dict(sorted(donors.items(), key=lambda pair: pair[1]["recorded_at"])[-32:])}
        with self.lock:
            if self.dsn:
                with psycopg.connect(self.dsn) as conn:
                    conn.execute("INSERT INTO semora_meta.database_capability_profiles(profile_key,descriptor,payload) VALUES(%s,%s::jsonb,'{}') ON CONFLICT DO NOTHING", (key, json.dumps(descriptor)))
                    row = conn.execute("SELECT payload FROM semora_meta.database_capability_profiles WHERE profile_key=%s FOR UPDATE", (key,)).fetchone()
                    value = update(row[0])
                    conn.execute("UPDATE semora_meta.database_capability_profiles SET payload=%s::jsonb,updated_at=now() WHERE profile_key=%s", (json.dumps(value), key))
            else:
                self.values[key] = update(deepcopy(self.values.get(key, {})))

    def resolve(self, kind, connection, capabilities=None):
        descriptor = profile_descriptor(kind, connection, capabilities)
        if descriptor is None:
            return None
        key = profile_key(descriptor)
        with self.lock:
            if self.dsn:
                with psycopg.connect(self.dsn) as conn:
                    row = conn.execute("SELECT payload FROM semora_meta.database_capability_profiles WHERE profile_key=%s", (key,)).fetchone()
                value = row[0] if row else {}
            else:
                value = deepcopy(self.values.get(key, {}))
        now = self.clock()
        threshold = now - timedelta(hours=max(1, int(os.getenv("SQL_CAPABILITY_CACHE_TTL_HOURS", "168"))))
        donors = [{**d, "probes": {k: v for k, v in d["probes"].items() if datetime.fromisoformat(v.get("observed_at", d["recorded_at"])) >= threshold}}
                  for d in value.get("donors", {}).values()]
        donors = [d for d in donors if d["probes"]]
        if not donors:
            return None
        shared, exact = {}, {}
        for donor in sorted(donors, key=lambda d: d["recorded_at"]):
            match = bool(connection.get("connection_fingerprint")) and donor.get("instance_key") == connection["connection_fingerprint"]
            for name, probe in donor["probes"].items():
                if name in shared and shared[name]["status"] != probe["status"]:
                    shared[name] = {"status": "mixed"}
                else:
                    shared.setdefault(name, deepcopy(probe))
                if match:
                    exact[name] = {**deepcopy(probe), "method": "instance_cache"}
        expires = min(datetime.fromisoformat(v.get("observed_at", d["recorded_at"])) for d in donors for v in d["probes"].values()) + (now - threshold)
        def bound(probes):
            recent = sorted(((k, v) for k, v in probes.items() if k.startswith("observed:")), key=lambda pair: pair[1].get("observed_at", ""))[-64:]
            return {**{k: v for k, v in probes.items() if not k.startswith("observed:")}, **dict(recent)}
        shared, exact = bound(shared), bound(exact)
        return {"profile_key": key, "descriptor": descriptor, "shared_probes": shared,
                "instance_probes": exact, "expires_at": expires.isoformat()}


def shared_probes(scan_result, now=None):
    cache = scan_result.get("capability_cache") or {}
    try:
        if datetime.fromisoformat(cache["expires_at"]) <= (now or datetime.now(timezone.utc)):
            return {}
    except (KeyError, ValueError, TypeError):
        return {}
    return cache.get("shared_probes", {})


def inherited_probes(scan_result, now=None):
    return {k: {**v, "method": "shared_baseline"} for k, v in shared_probes(scan_result, now).items()
            if v.get("status") == "verified"}


def effective_probes(scan_result):
    result = inherited_probes(scan_result)
    local = {**scan_result.get("connection", {}).get("probes", {}), **scan_result.get("sql_observations", {})}
    for name, probe in local.items():
        if not isinstance(probe, dict): continue
        if probe.get("expires_at"):
            try:
                if datetime.fromisoformat(probe["expires_at"]) <= datetime.now(timezone.utc): continue
            except (TypeError, ValueError): continue
        if probe.get("status") in {"verified", "unsupported"} or name not in result:
            result[name] = probe
    return result
