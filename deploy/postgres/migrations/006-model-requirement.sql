BEGIN;
ALTER TABLE semora_meta.sessions ADD COLUMN IF NOT EXISTS requirement_form JSONB NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS semora_meta.model_settings (
 id SMALLINT PRIMARY KEY CHECK (id=1),
 payload JSONB NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT,INSERT,UPDATE ON semora_meta.model_settings TO :api_role;
COMMIT;
