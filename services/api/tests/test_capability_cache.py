import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.capability_cache import CapabilityCache, inherited_probes, profile_descriptor
from app.capability_service import validate_cached
from app.database_profile import source_profile
from app.datasources import DataSourceRecord, DataSourceRepository
from semora_contracts.sql_probes import SMOKE_PROBES, probe_catalog, probe_matches


def connection(instance="account-a", version="8.0.45", **extra):
    return {"product": "MySQL", "version": version, "driver": "PyMySQL", "driver_version": "1.1.2",
            "dialect": "mysql", "read_only": True, "connection_fingerprint": instance, **extra}


def test_shared_baseline_and_exact_account_evidence_are_separate():
    cache = CapabilityCache()
    cache.publish("mysql", connection(), {}, "a", {"cte": {"status": "verified"}, "fn.abs.integer": {"status": "unsupported"}})
    other = cache.resolve("mysql", connection("account-b"))
    assert other["shared_probes"]["cte"]["status"] == "verified"
    assert not other["instance_probes"]
    own = cache.resolve("mysql", connection())
    assert own["instance_probes"]["fn.abs.integer"]["status"] == "unsupported"
    assert cache.resolve("mysql", connection(version="8.0.46")) is None
    assert cache.resolve("mysql", connection(driver_version="1.2")) is None
    assert cache.resolve("mysql", connection(compatibility_mode="other")) is None
    assert cache.resolve("mysql", connection(adapter_fingerprint="changed")) is None


def test_cache_conflicts_are_unknown_in_shared_baseline_but_keep_instance_override():
    cache = CapabilityCache()
    cache.publish("mysql", connection(), {}, "a", {"cte": {"status": "verified"}})
    cache.publish("mysql", connection("account-b"), {}, "b", {"cte": {"status": "unsupported"}})
    result = cache.resolve("mysql", connection("account-c"))
    assert result["shared_probes"]["cte"]["status"] == "mixed"
    assert "cte" not in inherited_probes({"capability_cache": result})
    assert cache.resolve("mysql", connection())["instance_probes"]["cte"]["status"] == "verified"


def test_cache_expiration_is_per_probe_and_reuse_does_not_refresh_evidence():
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    cache = CapabilityCache(clock=lambda: clock[0])
    cache.publish("mysql", connection(), {}, "a", {"cte": {"status": "verified"}})
    clock[0] += timedelta(days=6)
    cache.publish("mysql", connection(), {}, "a", {"fn.abs.integer": {"status": "verified"}})
    clock[0] += timedelta(days=2)
    result = cache.resolve("mysql", connection())
    assert "cte" not in result["shared_probes"]
    assert "fn.abs.integer" in result["shared_probes"]
    cache.publish("mysql", connection(), {}, "b", {"cte": {"status": "verified", "method": "instance_cache"}})
    assert "cte" not in cache.resolve("mysql", connection())["shared_probes"]


def test_missing_version_is_not_a_reusable_profile():
    assert profile_descriptor("mysql", {"version": "unknown"}) is None


def test_first_source_full_second_source_smoke_third_same_connection_reuses_all():
    repo = DataSourceRepository(dsn="")
    calls = []
    accounts = {"a": "one", "b": "two", "c": "two"}
    async def request(method, path, json):
        result = connection(accounts[json["datasource_id"]])
        if path.endswith("validate-query"):
            keys = json["probe_keys"]; calls.append(keys)
            result["probes"] = {key: {"status": "verified", "method": "synthetic_readonly_probe"} for key in keys}
            result["capabilities"] = {"query": "verified", "dialect": "mysql"}
        return result
    results = []
    for key in accounts:
        item = repo.save(DataSourceRecord(key, key, "mysql", "DATASOURCE_TEST_URL"))
        results.append(asyncio.run(validate_cached(item, repo, request)))
    assert len(calls[0]) == len(probe_catalog("mysql")) == 24
    assert set(calls[1]) == set(SMOKE_PROBES)
    assert calls[2] == []
    assert [r["probe_cache"]["mode"] for r in results] == ["full", "shared", "instance"]
    assert results[1]["probe_cache"]["reused"] == 20
    assert results[2]["probe_cache"]["reused"] == 24
    assert source_profile(repo.get("b"))["features"]["common_table_expressions"] is True
    asyncio.run(validate_cached(repo.get("b"), repo, request, force=True))
    assert len(calls[-1]) == 24


def test_new_source_does_not_inherit_a_global_negative_but_local_negative_wins():
    cache = CapabilityCache()
    cache.publish("mysql", connection(), {}, "a", {"cte": {"status": "unsupported"}})
    source = DataSourceRecord("b", "b", "mysql", "DATASOURCE_TEST_URL", scan_result={
        "connection": connection("other"), "capability_cache": cache.resolve("mysql", connection("other")),
    })
    assert source_profile(source)["features"]["common_table_expressions"] is None
    source.scan_result["connection"]["probes"] = {"cte": {"status": "unsupported"}}
    assert source_profile(source)["features"]["common_table_expressions"] is False


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "oracle", "tsql"])
def test_probe_catalog_is_synthetic_and_has_dialect_specific_sql(dialect):
    import sqlglot
    for key, spec in probe_catalog(dialect).items():
        if "sql" not in spec: continue
        tree = sqlglot.parse_one(spec["sql"], read=dialect)
        tables = {t.name.lower() for t in tree.find_all(sqlglot.exp.Table)}
        assert tables <= {"dual", "semora_probe", "semora_page"}
        assert spec["scope"] == "synthetic_signature"
    if dialect == "oracle":
        assert "DUAL" in probe_catalog(dialect)["fn.abs.integer"]["sql"]


def test_probe_comparison_checks_expected_values_and_null_not_only_execution_success():
    catalog = probe_catalog("mysql")
    assert probe_matches((5,), catalog["fn.abs.integer"])
    assert not probe_matches((4,), catalog["fn.abs.integer"])
    assert probe_matches((None,), catalog["fn.nullif.integer"])
    assert probe_matches(("123.45", None), catalog["types"])
    assert not probe_matches(("123.45", "not-null"), catalog["types"])


def test_concurrent_sources_do_not_both_repeat_full_catalog():
    repo = DataSourceRepository(dsn="")
    calls = []
    async def request(method, path, json):
        result = connection(json["datasource_id"])
        if path.endswith("validate-query"):
            keys = json["probe_keys"]
            calls.append(keys)
            await asyncio.sleep(0.01)
            result["probes"] = {key: {"status": "verified"} for key in keys}
        return result
    async def work():
        items = [repo.save(DataSourceRecord(k, k, "mysql", "DATASOURCE_TEST_URL")) for k in ("a", "b")]
        await asyncio.gather(*(validate_cached(item, repo, request) for item in items))
    asyncio.run(work())
    assert sorted(map(len, calls)) == [4, 24]


def test_learning_is_bounded_and_does_not_export_sql_or_literal_values(monkeypatch):
    import json
    from app import sql_learning
    repo = DataSourceRepository(dsn="")
    item = repo.save(DataSourceRecord("a", "a", "mysql", "DATASOURCE_TEST_URL",
        scan_result={"connection": connection()}))
    monkeypatch.setattr(sql_learning, "datasource_service", SimpleNamespace(repository=repo))
    step = SimpleNamespace(engine="source", data_source_id="a",
        sql="WITH unused AS (SELECT LOWER('hidden-detail')) SELECT UPPER('private-text') AS label")
    sql_learning.record_sql_success(step, SimpleNamespace(row_count=0))
    assert "sql_observations" not in item.scan_result
    sql_learning.record_sql_success(step, SimpleNamespace(row_count=1))
    observations = repo.get("a").scan_result["sql_observations"]
    assert len(observations) == 1
    assert next(iter(observations.values()))["function"] == "UPPER"
    exported = json.dumps(repo.capability_cache.resolve("mysql", connection()))
    assert "private-text" not in exported and "hidden-detail" not in exported
    repo.capability_cache.publish("mysql", connection(), {}, "a", {
        "observed:" + str(i): {"status": "verified", "scope": "observed_expression"} for i in range(90)})
    assert sum(key.startswith("observed:") for key in repo.capability_cache.resolve("mysql", connection())["shared_probes"]) == 64
