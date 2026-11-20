BEGIN;
-- Task intermediates only. These tables never contain published ontology relations.
CREATE TABLE IF NOT EXISTS semora_meta.relationship_analysis_work (
 job_id VARCHAR(64) NOT NULL REFERENCES semora_meta.datasource_onboarding_jobs(id) ON DELETE CASCADE,
 input_hash VARCHAR(64) NOT NULL,
 kind VARCHAR(40) NOT NULL,
 work_key VARCHAR(64) NOT NULL,
 status VARCHAR(16) NOT NULL DEFAULT 'running',
 owner VARCHAR(64),
 lease_until TIMESTAMPTZ,
 payload JSONB,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(job_id,input_hash,kind,work_key),
 CHECK(status IN ('running','completed','failed'))
);
CREATE TABLE IF NOT EXISTS semora_meta.relationship_analysis_keywords (
 job_id VARCHAR(64) NOT NULL REFERENCES semora_meta.datasource_onboarding_jobs(id) ON DELETE CASCADE,
 input_hash VARCHAR(64) NOT NULL,
 normalized_keyword TEXT NOT NULL,
 keyword TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(job_id,input_hash,normalized_keyword)
);
CREATE INDEX IF NOT EXISTS relationship_work_updated ON semora_meta.relationship_analysis_work(updated_at);
GRANT SELECT,INSERT,UPDATE,DELETE ON semora_meta.relationship_analysis_work,
 semora_meta.relationship_analysis_keywords TO :"api_role";
COMMIT;
