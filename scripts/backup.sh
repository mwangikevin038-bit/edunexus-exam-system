#!/bin/sh
# EduNexus daily backup: PostgreSQL dump + media uploads.
# Usage:  sh scripts/backup.sh [backup-dir]
# Cron:   0 2 * * * cd /opt/edunexus && sh scripts/backup.sh /opt/backups >> /var/log/edunexus-backup.log 2>&1
set -eu

BACKUP_DIR="${1:-$(pwd)/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Dump the database from inside the db container.
docker compose exec -T db sh -c \
  'pg_dump -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-school_exam_db}" --clean --if-exists' \
  > "$BACKUP_DIR/db-$STAMP.sql"

# Media uploads (signatures, profile pictures).
docker run --rm -v edunexus_media:/media:ro -v "$BACKUP_DIR":/out alpine \
  tar czf "/out/media-$STAMP.tar.gz" -C /media .

# Keep the newest 14 of each, prune the rest.
ls -1t "$BACKUP_DIR"/db-*.sql    2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t "$BACKUP_DIR"/media-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm -f

echo "Backup complete: $BACKUP_DIR/db-$STAMP.sql + media-$STAMP.tar.gz"
