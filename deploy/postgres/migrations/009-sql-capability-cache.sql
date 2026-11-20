BEGIN;
CREATE TABLE IF NOT EXISTS semora_meta.sql_capability_baselines (
    profile_key TEXT PRIMARY KEY,
    descriptor JSONB NOT NULL,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON semora_meta.sql_capability_baselines TO :"api_role";
COMMIT;
