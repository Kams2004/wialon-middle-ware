#!/usr/bin/env bash
# Consistent copy of the SQLite outbox to ./backups/ (safe while running).
source "$(dirname "$0")/_common.sh"
mkdir -p backups
name="bridge-$(date +%Y%m%d-%H%M%S).db"
in_container <<'PY'
import sqlite3
src = sqlite3.connect("/app/data/bridge.db")
dst = sqlite3.connect("/app/data/backup.tmp")
src.backup(dst)
dst.close(); src.close()
PY
docker cp wialon-middleware:/app/data/backup.tmp "backups/$name"
$COMPOSE exec -T middleware rm -f /app/data/backup.tmp
echo "saved backups/$name"
