#!/bin/sh
# Daily compressed logical backup with retention. Restore with:
#   gunzip -c backups/wae-YYYYmmdd-HHMMSS.sql.gz | psql -h <host> -U wae_owner wae
# Backups contain encrypted credential envelopes; they are useless without WAE_MASTER_KEYS,
# which must be backed up separately (and never alongside the database dumps).
set -eu
KEEP="${BACKUP_KEEP_DAYS:-14}"
while true; do
  STAMP=$(date -u +%Y%m%d-%H%M%S)
  FILE="/backups/wae-${STAMP}.sql.gz"
  if pg_dump -h postgres -U wae_owner -d wae --no-owner | gzip > "${FILE}.tmp"; then
    mv "${FILE}.tmp" "${FILE}"
    echo "backup written: ${FILE}"
  else
    rm -f "${FILE}.tmp"
    echo "backup FAILED at ${STAMP}" >&2
  fi
  find /backups -name 'wae-*.sql.gz' -mtime +"${KEEP}" -delete
  sleep 86400
done
