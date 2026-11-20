#!/bin/sh
# Transactional, checksum-verified migrations for fresh compiled-delivery installs.
set -eu
: "${METADATA_DATABASE_URL:?METADATA_DATABASE_URL is required}"
: "${METADATA_API_ROLE:?METADATA_API_ROLE is required}"
case "$METADATA_API_ROLE" in *[!a-zA-Z0-9_]*|'') echo 'Invalid database role' >&2; exit 64;; esac
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cat > "$work/driver.sql" <<'SQL'
SELECT pg_advisory_xact_lock(782441, 1);
DO $$ BEGIN
  IF to_regclass('semora_meta.sessions') IS NOT NULL
     AND to_regclass('semra_delivery.migrations') IS NULL THEN
    RAISE EXCEPTION 'Existing untracked metadata database: restore or baseline explicitly; refusing to replay historical cleanup';
  END IF;
END $$;
CREATE SCHEMA IF NOT EXISTS semra_delivery;
REVOKE ALL ON SCHEMA semra_delivery FROM PUBLIC;
CREATE TABLE IF NOT EXISTS semra_delivery.migrations (
  name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
);
SQL
for file in /migrations/*.sql; do
  name=$(basename "$file")
  checksum=$(sha256sum "$file" | cut -d ' ' -f 1)
  # Historical files own their transactions. The delivery runner owns one atomic
  # transaction for their contents and ledger instead, including upgrades.
  sed -e '/^BEGIN;$/d' -e '/^COMMIT;$/d' "$file" > "$work/$name"
  cat >> "$work/driver.sql" <<SQL
SELECT EXISTS (SELECT 1 FROM semra_delivery.migrations WHERE name='$name') AS applied \gset
\if :applied
  SELECT sha256='$checksum' AS checksum_ok FROM semra_delivery.migrations WHERE name='$name' \gset
  \if :checksum_ok
    \echo Skipping $name
  \else
    \echo Migration checksum mismatch: $name
    DO \$\$ BEGIN RAISE EXCEPTION 'Migration checksum mismatch: $name'; END \$\$;
  \endif
\else
  \echo Applying $name
  \i $work/$name
  INSERT INTO semra_delivery.migrations(name,sha256) VALUES ('$name','$checksum');
\endif
SQL
done
psql "$METADATA_DATABASE_URL" -X -v ON_ERROR_STOP=1 -v api_role="$METADATA_API_ROLE" \
  --single-transaction -f "$work/driver.sql"
