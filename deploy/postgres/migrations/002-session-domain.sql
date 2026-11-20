BEGIN;

ALTER TABLE semora_meta.sessions
    ADD COLUMN IF NOT EXISTS domain VARCHAR(100) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS sessions_domain_created_idx
    ON semora_meta.sessions(domain, created_at DESC);

COMMIT;
