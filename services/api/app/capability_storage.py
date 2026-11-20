"""Persistence boundary: schema scans and database capability evidence are stored separately.

The runtime snapshot is a compatibility view for existing query consumers. The JSON
written to datasources.scan_result contains no capability metadata or probe copies.
"""
from copy import deepcopy
import json

CAPABILITY_KEYS = {"connection", "capabilities", "capability_cache", "sql_observations"}


def split_snapshot(snapshot):
    scan = {k: deepcopy(v) for k, v in snapshot.items() if k not in CAPABILITY_KEYS}
    connection = deepcopy(snapshot.get("connection") or {})
    probes = connection.pop("probes", {})
    capabilities = deepcopy(snapshot.get("capabilities") or {})
    # record_test exposes probes in both places for API compatibility; store once.
    for key in probes:
        capabilities.pop(key, None)
    receipt = {k: deepcopy(v) for k, v in (snapshot.get("capability_cache") or {}).items()
               if k not in {"shared_probes", "instance_probes"}}
    return scan, (connection, capabilities, probes,
                  deepcopy(snapshot.get("sql_observations") or {}), receipt)


def join_snapshot(scan, connection, capabilities, probes, observations, receipt):
    result = deepcopy(scan)
    if connection or probes:
        result["connection"] = {**deepcopy(connection), **({"probes": deepcopy(probes)} if probes else {})}
    if capabilities or probes:
        result["capabilities"] = {**deepcopy(capabilities), **deepcopy(probes)}
    if observations:
        result["sql_observations"] = deepcopy(observations)
    if receipt:
        result["capability_cache"] = deepcopy(receipt)
    return result


def write_observations(cursor, source_id, state):
    connection, capabilities, probes, observations, receipt = state
    cursor.execute("""INSERT INTO semora_meta.datasource_capability_observations
        (datasource_id,environment_fingerprint,connection_metadata,capabilities,probes,observations,reuse_receipt)
        VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb)
        ON CONFLICT(datasource_id) DO UPDATE SET
        environment_fingerprint=excluded.environment_fingerprint,
        connection_metadata=excluded.connection_metadata,capabilities=excluded.capabilities,
        probes=excluded.probes,observations=excluded.observations,reuse_receipt=excluded.reuse_receipt,
        updated_at=now()""", (source_id, connection.get("connection_fingerprint"),
        *(json.dumps(v, ensure_ascii=False) for v in state)))
