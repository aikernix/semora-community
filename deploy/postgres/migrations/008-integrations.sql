BEGIN;
CREATE TABLE IF NOT EXISTS semora_meta.integrations (
    name TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON semora_meta.integrations TO :"api_role";
COMMIT;
