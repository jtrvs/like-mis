#!/bin/sh
# Резервное копирование PostgreSQL: дамп каждые BACKUP_INTERVAL секунд, хранится BACKUP_KEEP последних.
# RPO (сколько данных можно потерять при сбое) = BACKUP_INTERVAL, по умолчанию 15 минут.
# Ошибка одного дампа (например, база перезапускается) НЕ останавливает цикл — следующий будет через интервал.
INTERVAL="${BACKUP_INTERVAL:-900}"
KEEP="${BACKUP_KEEP:-96}"
DIR="${BACKUP_DIR:-/backups}"
mkdir -p "$DIR"
while true; do
  TS=$(date +%Y%m%d_%H%M%S)
  START=$(date +%s)
  if PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h "${POSTGRES_HOST:-db}" -p "${POSTGRES_PORT:-5432}" -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$DIR/lambda_$TS.dump.tmp"; then
    mv "$DIR/lambda_$TS.dump.tmp" "$DIR/lambda_$TS.dump"
    echo "$(date -Iseconds) backup ok: lambda_$TS.dump ($(du -h "$DIR/lambda_$TS.dump" | cut -f1), $(( $(date +%s) - START )) с)"
    ls -1t "$DIR"/lambda_*.dump | tail -n +$((KEEP + 1)) | xargs -r rm -f
  else
    echo "$(date -Iseconds) BACKUP FAILED — повторю через ${INTERVAL} с" >&2
    rm -f "$DIR/lambda_$TS.dump.tmp"
  fi
  sleep "$INTERVAL"
done
