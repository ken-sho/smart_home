#!/bin/bash
# Бэкап всех PostgreSQL баз в Яндекс Диск
# Глубина 7 дней — файлы именуются по дню недели
# При совпадении старый файл перезаписывается новым

BACKUP_DIR=/data/backups/db
DOW=$(date +%a | tr '[:upper:]' '[:lower:]')
REMOTE="yadisk:backups/core-db"

mkdir -p $BACKUP_DIR

echo "Starting DB backup: $(date '+%Y-%m-%d %H:%M') [$DOW]"

sudo -u postgres pg_dumpall > $BACKUP_DIR/core_${DOW}.sql
echo "Done: core_${DOW}.sql ($(du -sh $BACKUP_DIR/core_${DOW}.sql | cut -f1))"

echo "Uploading to Яндекс Диск..."
rclone copy $BACKUP_DIR $REMOTE --include "core_${DOW}.sql"

echo "DB backup complete: $(date '+%Y-%m-%d %H:%M') [$DOW]"
