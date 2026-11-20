BEGIN;

CREATE TABLE IF NOT EXISTS semora_meta.datasource_tables (
    datasource_id VARCHAR(64) NOT NULL REFERENCES semora_meta.datasources(id) ON DELETE CASCADE,
    table_id VARCHAR(512) NOT NULL,
    ordinal INTEGER NOT NULL,
    catalog_name TEXT,
    schema_name TEXT,
    object_name TEXT,
    table_name TEXT,
    object_kind VARCHAR(32),
    description TEXT,
    columns JSONB,
    primary_keys JSONB,
    unique_constraints JSONB,
    foreign_keys JSONB,
    indexes JSONB,
    extra JSONB NOT NULL DEFAULT '{}'::jsonb,
    present_fields JSONB NOT NULL DEFAULT '[]'::jsonb,
    scanned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash VARCHAR(64) NOT NULL,
    PRIMARY KEY (datasource_id, table_id)
);

CREATE INDEX IF NOT EXISTS idx_datasource_tables_source_name
    ON semora_meta.datasource_tables(datasource_id, object_name);
CREATE INDEX IF NOT EXISTS idx_datasource_tables_source_schema
    ON semora_meta.datasource_tables(datasource_id, schema_name);

-- Convert the former all-tables JSON once. Replays are harmless because the
-- source payload is removed only after this INSERT succeeds.
INSERT INTO semora_meta.datasource_tables(
    datasource_id,table_id,ordinal,catalog_name,schema_name,object_name,table_name,
    object_kind,description,columns,primary_keys,unique_constraints,foreign_keys,
    indexes,extra,present_fields,scanned_at,content_hash
)
SELECT d.id,
       COALESCE(NULLIF(t.value->>'id',''),
                NULLIF(concat_ws('.',NULLIF(t.value->>'catalog',''),NULLIF(t.value->>'schema',''),NULLIF(t.value->>'name','')),''),
                'legacy-table-' || (t.ordinality - 1)),
       t.ordinality - 1,
       t.value->>'catalog',t.value->>'schema',t.value->>'name',t.value->>'table_name',
       t.value->>'kind',t.value->>'description',
       CASE WHEN t.value ? 'columns' THEN t.value->'columns' END,
       CASE WHEN t.value ? 'primary_keys' THEN t.value->'primary_keys' END,
       CASE WHEN t.value ? 'unique_constraints' THEN t.value->'unique_constraints' END,
       CASE WHEN t.value ? 'foreign_keys' THEN t.value->'foreign_keys' END,
       CASE WHEN t.value ? 'indexes' THEN t.value->'indexes' END,
       t.value - ARRAY['id','catalog','schema','name','table_name','kind','description',
                       'columns','primary_keys','unique_constraints','foreign_keys','indexes']::text[],
       to_jsonb(ARRAY(
           SELECT key FROM jsonb_object_keys(t.value) AS key
           WHERE key = ANY(ARRAY['id','catalog','schema','name','table_name','kind','description',
                                 'columns','primary_keys','unique_constraints','foreign_keys','indexes'])
       )),
       COALESCE(d.last_scan_at,now()),
       md5(t.value::text)
FROM semora_meta.datasources d
CROSS JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(d.scan_result->'tables')='array'
         THEN d.scan_result->'tables' ELSE '[]'::jsonb END
) WITH ORDINALITY AS t(value,ordinality)
ON CONFLICT(datasource_id,table_id) DO NOTHING;

UPDATE semora_meta.datasources
SET scan_result = (scan_result - 'tables') || '{"_schema_tables_present":true}'::jsonb
WHERE scan_result ? 'tables';

GRANT SELECT, INSERT, UPDATE, DELETE ON semora_meta.datasource_tables TO :"api_role";

COMMIT;
