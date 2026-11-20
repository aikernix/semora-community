BEGIN;
CREATE TABLE IF NOT EXISTS semora_meta.datasource_relationship_inputs (
 datasource_id VARCHAR(64) NOT NULL REFERENCES semora_meta.datasources(id) ON DELETE CASCADE,
 table_id VARCHAR(512) NOT NULL,
 input_hash VARCHAR(64) NOT NULL,
 format_version VARCHAR(64) NOT NULL,
 payload JSONB NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(datasource_id,table_id)
);
GRANT SELECT,INSERT,UPDATE,DELETE ON semora_meta.datasource_relationship_inputs TO :"api_role";
-- Keep exactly the currently published record IDs; never reactivate historical accepted candidates.
UPDATE semora_meta.datasource_ontology_packages p SET payload=jsonb_set(p.payload,'{relationshipRecords}',
 COALESCE((SELECT jsonb_agg(to_jsonb(c)) FROM semora_meta.relationship_candidates c
 WHERE c.status='accepted' AND c.scope='single_source' AND c.source_datasource_id=p.datasource_id
 AND EXISTS(SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(p.payload->'relations')='array' THEN p.payload->'relations' ELSE '[]'::jsonb END) r WHERE r->'metadata'->>'candidateId'=c.id)), '[]'::jsonb))
WHERE NOT p.payload ? 'relationshipRecords';
UPDATE semora_meta.datasource_ontology_packages SET status='ready',pending_count=0 WHERE status='ready_with_pending';
COMMIT;
-- Retain the legacy pending_count column for rollback compatibility; new APIs omit it.
