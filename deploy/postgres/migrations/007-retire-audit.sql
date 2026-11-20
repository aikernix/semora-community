-- The SQL-native runtime no longer reads/writes the retired audit ledger.
-- Application deployments must back up existing data before this migration.
BEGIN;
ALTER TABLE semora_meta.sessions ALTER COLUMN domain SET DEFAULT '';
DROP TABLE IF EXISTS semora_meta.audit_archives;
DROP TABLE IF EXISTS semora_meta.audit_events;
DROP FUNCTION IF EXISTS semora_meta.validate_audit_append();
DROP FUNCTION IF EXISTS semora_meta.reject_audit_mutation();
DROP FUNCTION IF EXISTS semora_meta.reject_audit_archive_mutation();
COMMIT;
