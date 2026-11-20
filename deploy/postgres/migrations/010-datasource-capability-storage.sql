BEGIN;

CREATE TABLE IF NOT EXISTS semora_meta.database_capability_profiles (
    profile_key TEXT PRIMARY KEY,
    descriptor JSONB NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Preserve previously deployed shared-cache evidence before retiring its old name.
DO $$ BEGIN
IF to_regclass('semora_meta.sql_capability_baselines') IS NOT NULL THEN
    INSERT INTO semora_meta.database_capability_profiles(profile_key, descriptor, payload, updated_at)
    SELECT profile_key, descriptor, payload, updated_at
    FROM semora_meta.sql_capability_baselines
    ON CONFLICT(profile_key) DO UPDATE SET descriptor=excluded.descriptor,
        payload=excluded.payload, updated_at=excluded.updated_at
    WHERE excluded.updated_at > database_capability_profiles.updated_at;
END IF;
END $$;

ALTER TABLE semora_meta.datasources ADD COLUMN IF NOT EXISTS capability_profile_id TEXT
    REFERENCES semora_meta.database_capability_profiles(profile_key);
CREATE INDEX IF NOT EXISTS datasource_capability_profile_idx
    ON semora_meta.datasources(capability_profile_id);

CREATE TABLE IF NOT EXISTS semora_meta.datasource_capability_observations (
    datasource_id VARCHAR(64) PRIMARY KEY REFERENCES semora_meta.datasources(id) ON DELETE CASCADE,
    environment_fingerprint TEXT,
    connection_metadata JSONB NOT NULL DEFAULT '{}',
    capabilities JSONB NOT NULL DEFAULT '{}',
    probes JSONB NOT NULL DEFAULT '{}',
    observations JSONB NOT NULL DEFAULT '{}',
    reuse_receipt JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Move local capability evidence out of the table-structure scan snapshot.
-- Only insert missing records: rerunning migrations must not overwrite newer evidence.
WITH legacy AS (
    SELECT id,
        CASE WHEN jsonb_typeof(scan_result->'connection')='object' THEN scan_result->'connection' ELSE '{}'::jsonb END AS connection,
        CASE WHEN jsonb_typeof(scan_result->'capabilities')='object' THEN scan_result->'capabilities' ELSE '{}'::jsonb END AS capabilities,
        CASE WHEN jsonb_typeof(scan_result #> '{connection,probes}')='object' THEN scan_result #> '{connection,probes}' ELSE '{}'::jsonb END AS probes,
        CASE WHEN jsonb_typeof(scan_result->'sql_observations')='object' THEN scan_result->'sql_observations' ELSE '{}'::jsonb END AS observations,
        CASE WHEN jsonb_typeof(scan_result->'capability_cache')='object' THEN scan_result->'capability_cache' ELSE '{}'::jsonb END AS receipt
    FROM semora_meta.datasources
    WHERE scan_result ?| ARRAY['connection','capabilities','capability_cache','sql_observations']
)
INSERT INTO semora_meta.datasource_capability_observations
    (datasource_id,environment_fingerprint,connection_metadata,capabilities,probes,observations,reuse_receipt)
SELECT id,connection->>'connection_fingerprint',connection - 'probes',
    capabilities - ARRAY(SELECT jsonb_object_keys(probes)), probes, observations,
    receipt - ARRAY['shared_probes','instance_probes']
FROM legacy
ON CONFLICT(datasource_id) DO NOTHING;

UPDATE semora_meta.datasources d SET capability_profile_id=p.profile_key
FROM semora_meta.database_capability_profiles p
WHERE d.capability_profile_id IS NULL
 AND d.scan_result #>> '{capability_cache,profile_key}' = p.profile_key;

UPDATE semora_meta.datasources SET scan_result = scan_result
    - ARRAY['connection','capabilities','capability_cache','sql_observations'];

GRANT SELECT, INSERT, UPDATE ON semora_meta.database_capability_profiles TO :"api_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON semora_meta.datasource_capability_observations TO :"api_role";
DROP TABLE IF EXISTS semora_meta.sql_capability_baselines;
COMMIT;
