"""Capability persistence must preserve query semantics without polluting schema scans."""
from copy import deepcopy
from app.capability_storage import split_snapshot, join_snapshot
from app.datasources import DataSourceRepository, DataSourceRecord
from app.database_profile import source_profile
from test_capability_cache import connection


def test_storage_round_trip_preserves_local_override_and_removes_shared_copies():
    snapshot = {"tables": [{"name": "items", "columns": [{"name": "id"}]}],
        "connection": {**connection(), "probes": {"cte": {"status": "unsupported", "code": 1064}}},
        "capabilities": {"query": "verified", "cte": {"status": "unsupported", "code": 1064}},
        "sql_observations": {"observed:x": {"status": "verified", "function": "ABS"}},
        "capability_cache": {"profile_key": "p", "mode": "shared", "shared_probes": {"cte": {"status": "verified"}},
            "instance_probes": {"cte": {"status": "unsupported"}}}}
    before = deepcopy(snapshot)
    scan, state = split_snapshot(snapshot)
    assert snapshot == before
    assert scan == {"tables": before["tables"]}
    assert "cte" not in state[1]  # one canonical copy of local probe evidence
    assert "shared_probes" not in state[4] and "instance_probes" not in state[4]
    restored = join_snapshot(scan, *state)
    assert source_profile(DataSourceRecord("x", "x", "mysql", "REF", scan_result=restored))["features"]["common_table_expressions"] is False
    assert restored["connection"] == snapshot["connection"]
    assert restored["sql_observations"] == snapshot["sql_observations"]


def test_profile_association_is_shared_across_sources_and_invalidated_when_identity_changes():
    repo = DataSourceRepository(dsn="")
    a = repo.save(DataSourceRecord("a", "a", "mysql", "REF", scan_result={"connection": connection("a")}))
    b = repo.save(DataSourceRecord("b", "b", "mysql", "REF", scan_result={"connection": connection("b")}))
    assert a.capability_profile_id and a.capability_profile_id == b.capability_profile_id
    assert b.public()["capability_profile_id"] == a.capability_profile_id
    repo.record_test("b", {**connection("b", version="8.0.46"), "read_only": True})
    assert a.capability_profile_id != b.capability_profile_id
    b.scan_result = {}
    repo.save(b)
    assert b.capability_profile_id is None
