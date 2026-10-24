#!/bin/sh
# Восстановление: ./ops/restore.sh [файл.dump]  (по умолчанию — последний). Печатает время восстановления (RTO).
set -eu
FILE="${1:-$(ls -1t ${BACKUP_DIR:-/backups}/lambda_*.dump | head -1)}"
START=$(date +%s)
PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -h "${POSTGRES_HOST:-db}" -p "${POSTGRES_PORT:-5432}" -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists "$FILE"
echo "restored $FILE за $(( $(date +%s) - START )) с"
