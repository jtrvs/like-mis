#!/bin/sh
# Резервное копирование PostgreSQL: дамп каждые BACKUP_INTERVAL секунд, хранение BACKUP_KEEP штук.
# RPO (максимум потерянных данных) = BACKUP_INTERVAL; для критичных событий см. README — алерты пишутся
# в БД в той же транзакции, что и данные пациента, и повторно отправляются после восстановления.
set -eu
INTERVAL="${BACKUP_INTERVAL:-900}"
KEEP="${BACKUP_KEEP:-96}"
DIR="${BACKUP_DIR:-/backups}"
mkdir -p "$DIR"
while true; do
  TS=$(date +%Y%m%d_%H%M%S)
  START=$(date +%s)
  PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h "${POSTGRES_HOST:-db}" -p "${POSTGRES_PORT:-5432}" -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$DIR/lambda_$TS.dump.tmp"
  mv "$DIR/lambda_$TS.dump.tmp" "$DIR/lambda_$TS.dump"
  echo "$(date -Iseconds) backup ok: lambda_$TS.dump ($(du -h "$DIR/lambda_$TS.dump" | cut -f1), $(( $(date +%s) - START )) с)"
  ls -1t "$DIR"/lambda_*.dump | tail -n +$((KEEP + 1)) | xargs -r rm -f
  sleep "$INTERVAL"
done
