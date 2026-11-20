BEGIN;

CREATE TABLE IF NOT EXISTS semora_meta.datasource_onboarding_jobs (
    id VARCHAR(64) PRIMARY KEY,
    datasource_id VARCHAR(64) NOT NULL REFERENCES semora_meta.datasources(id) ON DELETE CASCADE,
    kind VARCHAR(32) NOT NULL DEFAULT 'onboarding',
    peer_datasource_id VARCHAR(64) REFERENCES semora_meta.datasources(id) ON DELETE CASCADE,
    domain VARCHAR(100),
    status VARCHAR(40) NOT NULL,
    phase VARCHAR(40) NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    error TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_datasource_onboarding_jobs_source
    ON semora_meta.datasource_onboarding_jobs(datasource_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_datasource_onboarding_jobs_active
    ON semora_meta.datasource_onboarding_jobs(status) WHERE status NOT IN ('completed','completed_with_pending','failed','cancelled');

CREATE TABLE IF NOT EXISTS semora_meta.datasource_ontology_packages (
    datasource_id VARCHAR(64) PRIMARY KEY REFERENCES semora_meta.datasources(id) ON DELETE CASCADE,
    schema_hash VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    object_count INTEGER NOT NULL DEFAULT 0,
    relation_count INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    generator_version VARCHAR(40) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS semora_meta.relationship_candidates (
    id VARCHAR(64) PRIMARY KEY,
    scope VARCHAR(24) NOT NULL CHECK (scope IN ('single_source','cross_source')),
    source_datasource_id VARCHAR(64) NOT NULL REFERENCES semora_meta.datasources(id) ON DELETE CASCADE,
    target_datasource_id VARCHAR(64) NOT NULL REFERENCES semora_meta.datasources(id) ON DELETE CASCADE,
    source_table_id VARCHAR(512) NOT NULL,
    target_table_id VARCHAR(512) NOT NULL,
    source_columns JSONB NOT NULL,
    target_columns JSONB NOT NULL,
    key_pairs JSONB NOT NULL,
    condition_template JSONB NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_grade VARCHAR(24) NOT NULL,
    confidence NUMERIC(5,4) NOT NULL DEFAULT 0,
    cardinality VARCHAR(8) NOT NULL DEFAULT 'N:1',
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    source_schema_hash VARCHAR(64) NOT NULL,
    target_schema_hash VARCHAR(64) NOT NULL,
    profile_version VARCHAR(40) NOT NULL,
    domain VARCHAR(100),
    reviewed_by VARCHAR(200),
    review_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_relationship_candidates_source
    ON semora_meta.relationship_candidates(source_datasource_id,scope,status);
CREATE INDEX IF NOT EXISTS idx_relationship_candidates_target
    ON semora_meta.relationship_candidates(target_datasource_id,scope,status);
CREATE INDEX IF NOT EXISTS idx_relationship_candidates_domain
    ON semora_meta.relationship_candidates(domain,scope,status);

CREATE TABLE IF NOT EXISTS semora_meta.datasource_column_profiles (
    datasource_id VARCHAR(64) NOT NULL REFERENCES semora_meta.datasources(id) ON DELETE CASCADE,
    table_id VARCHAR(512) NOT NULL,
    columns JSONB NOT NULL,
    profile_key VARCHAR(64) NOT NULL,
    type_family VARCHAR(64),
    indexed BOOLEAN NOT NULL DEFAULT false,
    unique_key BOOLEAN NOT NULL DEFAULT false,
    nullable BOOLEAN NOT NULL DEFAULT true,
    statistics JSONB NOT NULL DEFAULT '{}'::jsonb,
    signature JSONB,
    signature_version VARCHAR(40),
    profiled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(datasource_id,table_id,profile_key)
);

-- Future source-code analysis plugs into the same evidence model.  This table
-- stores provider configuration only; no repository is cloned by this release.
CREATE TABLE IF NOT EXISTS semora_meta.relationship_evidence_sources (
    id VARCHAR(64) PRIMARY KEY,
    domain VARCHAR(100),
    provider_type VARCHAR(32) NOT NULL CHECK (provider_type IN ('source_code','manual','catalog')),
    configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON semora_meta.datasource_onboarding_jobs TO :"api_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON semora_meta.datasource_ontology_packages TO :"api_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON semora_meta.relationship_candidates TO :"api_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON semora_meta.datasource_column_profiles TO :"api_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON semora_meta.relationship_evidence_sources TO :"api_role";

COMMIT;
