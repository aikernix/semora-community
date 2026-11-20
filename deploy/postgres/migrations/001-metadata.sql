BEGIN;

CREATE SCHEMA IF NOT EXISTS semora_meta;
ALTER SCHEMA semora_meta OWNER TO CURRENT_USER;
REVOKE CREATE ON SCHEMA semora_meta FROM PUBLIC;
REVOKE CREATE ON SCHEMA semora_meta FROM :"api_role";
GRANT USAGE ON SCHEMA semora_meta TO :"api_role";

CREATE TABLE IF NOT EXISTS semora_meta.sessions (
    id VARCHAR(64) PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    domain VARCHAR(100) NOT NULL DEFAULT '',
    owner_subject VARCHAR(200),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS semora_meta.runs (
    id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL REFERENCES semora_meta.sessions(id),
    status VARCHAR(32) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS semora_meta.datasources (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    type VARCHAR(32) NOT NULL,
    secret_ref VARCHAR(200) NOT NULL,
    status VARCHAR(32) NOT NULL,
    access VARCHAR(32) NOT NULL,
    objects INTEGER NOT NULL DEFAULT 0,
    last_scan_at TIMESTAMPTZ,
    scan_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS semora_meta.system_settings (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    payload JSONB NOT NULL,
    version INTEGER NOT NULL,
    policy_version VARCHAR(96) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by VARCHAR(200) NOT NULL
);

GRANT SELECT, INSERT, UPDATE ON semora_meta.sessions TO :"api_role";
GRANT SELECT, INSERT, UPDATE ON semora_meta.runs TO :"api_role";
GRANT SELECT, INSERT, UPDATE ON semora_meta.datasources TO :"api_role";
GRANT SELECT, INSERT, UPDATE ON semora_meta.system_settings TO :"api_role";

COMMIT;
