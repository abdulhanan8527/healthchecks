#!/usr/bin/env bash
#
# Backup script for Healthchecks.
#
# Detects the configured database (SQLite by default, Postgres when DB=postgres)
# and writes a timestamped backup into BACKUP_DIR. Old backups beyond KEEP days
# are removed.
#
# Usage:
#   deploy/backup.sh
#
# Suggested cron (daily at 03:30):
#   30 3 * * * /home/muhammad-ans/webapps/healthchecks/deploy/backup.sh >> /var/log/healthchecks-backup.log 2>&1

set -euo pipefail

APP_DIR="/home/muhammad-ans/webapps/healthchecks"
PY="/home/muhammad-ans/webapps/.venv/bin/python"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP="${KEEP:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$BACKUP_DIR"

# Read DB settings from the Django settings module so we stay in sync.
DB_ENGINE="$("$PY" - <<'PYEOF'
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hc.settings")
django.setup()
from django.conf import settings
print(settings.DATABASES["default"]["ENGINE"])
PYEOF
)"

case "$DB_ENGINE" in
  *sqlite3)
    SRC="$APP_DIR/hc.sqlite"
    DEST="$BACKUP_DIR/hc-$STAMP.sqlite"
    echo "Backing up SQLite $SRC -> $DEST"
    # Use sqlite3's .backup for a consistent snapshot even while in use.
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "$SRC" ".backup '$DEST'"
    else
      "$PY" - "$SRC" "$DEST" <<'PYEOF'
import sqlite3, sys
src, dest = sys.argv[1], sys.argv[2]
con = sqlite3.connect(src)
out = sqlite3.connect(dest)
with out:
    con.backup(out)
con.close(); out.close()
PYEOF
    fi
    ;;
  *postgres*)
    DEST="$BACKUP_DIR/hc-$STAMP.sql.gz"
    echo "Backing up PostgreSQL -> $DEST"
    eval "$( "$PY" - <<'PYEOF'
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hc.settings")
django.setup()
from django.conf import settings
d = settings.DATABASES["default"]
print(f"export PGHOST={d.get('HOST','127.0.0.1')}")
print(f"export PGPORT={d.get('PORT','5432')}")
print(f"export PGDATABASE={d.get('NAME','hc')}")
print(f"export PGUSER={d.get('USER','hc')}")
if d.get("PASSWORD"):
    print(f"export PGPASSWORD={d['PASSWORD']}")
PYEOF
    )"
    pg_dump --no-owner --no-privileges | gzip > "$DEST"
    ;;
  *)
    echo "Unknown DB engine: $DB_ENGINE" >&2
    exit 1
    ;;
esac

echo "Pruning backups older than $KEEP days in $BACKUP_DIR"
find "$BACKUP_DIR" -name 'hc-*' -type f -mtime +"$KEEP" -print -delete || true

echo "Backup complete: $DEST"
