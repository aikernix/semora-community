#!/bin/sh
set -eu

: "${METADATA_DATABASE_URL:?METADATA_DATABASE_URL is required}"
: "${METADATA_API_ROLE:?METADATA_API_ROLE is required}"

for role in "$METADATA_API_ROLE"; do
case "$role" in
  *[!a-zA-Z0-9_]*|'')
    echo "database roles must be PostgreSQL identifiers" >&2
    exit 64
    ;;
esac
done

for file in /migrations/*.sql; do
  echo "Applying $(basename "$file")"
  psql "$METADATA_DATABASE_URL" \
    -v ON_ERROR_STOP=1 \
    -v api_role="$METADATA_API_ROLE" \
    -f "$file"
done
