-- Only relation research queues are retired; historical snapshots and scores are retained.
DELETE FROM semora_meta.relationship_candidates WHERE status IN ('pending','stale','rejected');
UPDATE semora_meta.datasource_ontology_packages p
SET payload=jsonb_set(p.payload,'{relationshipRecords}',
 COALESCE((SELECT jsonb_agg(r) FROM jsonb_array_elements(COALESCE(p.payload->'relationshipRecords','[]'::jsonb)) r WHERE r->>'status'='accepted'),'[]'::jsonb)),
 pending_count=0
WHERE p.payload ? 'relationshipRecords';
DO $$ BEGIN
 IF to_regclass('ontology_core.domain_cross_source_relations') IS NOT NULL THEN
  DELETE FROM ontology_core.domain_cross_source_relations;
 END IF;
 IF to_regclass('ontology_core.candidates') IS NOT NULL THEN
  DELETE FROM ontology_core.candidates WHERE kind='relation' AND status IN ('pending_review','rejected','stale');
 END IF;
END $$;
