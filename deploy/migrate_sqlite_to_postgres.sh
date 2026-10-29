#!/usr/bin/env bash
#
# One-time data migration: SQLite -> PostgreSQL for Healthchecks.
#
# It dumps every row from the current SQLite database, runs the schema
# migrations against an empty PostgreSQL database, then loads the dump in.
# Use it once, when cutting over from the dev-grade SQLite setup to Postgres
# (feature #14, "SQLite->PG migration"). For routine backups use backup.sh.
#
# The PostgreSQL *target* is read from the same environment variables that
# hc/settings.py uses, so this script never hardcodes credentials:
#
#   export DB=postgres
#   export DB_HOST=127.0.0.1
#   export DB_PORT=5432
#   export DB_NAME=hc
#   export DB_USER=hc
#   export DB_PASSWORD=********        # or DB_PASSWORD_FILE=/run/secrets/db
#
# Create the (empty) target database and role first, e.g.:
#   sudo -u postgres createuser hc
#   sudo -u postgres createdb -O hc hc
#
# Then run:
#   deploy/migrate_sqlite_to_postgres.sh            # asks for confirmation
#   deploy/migrate_sqlite_to_postgres.sh --yes      # non-interactive
#
# The target Postgres DB must be EMPTY: the script runs `migrate` to build the
# schema and then `loaddata`. Loading into a populated DB will collide on
# primary keys.

set -euo pipefail

APP_DIR="/home/muhammad-ans/webapps/healthchecks"
PY="/home/muhammad-ans/webapps/.venv/bin/python"
# Source SQLite file (override with SQLITE_PATH if you renamed it):
SQLITE_PATH="${SQLITE_PATH:-$APP_DIR/hc.sqlite}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP_FILE="${DUMP_FILE:-$APP_DIR/backups/sqlite-dump-$STAMP.json}"

ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd "$APP_DIR"
mkdir -p "$(dirname "$DUMP_FILE")"

if [ ! -f "$SQLITE_PATH" ]; then
  echo "Source SQLite database not found: $SQLITE_PATH" >&2
  exit 1
fi

# The Postgres target must be fully specified before we touch anything.
: "${DB_HOST:=}"
: "${DB_PORT:=5432}"
: "${DB_NAME:=}"
: "${DB_USER:=}"
if [ -z "$DB_NAME" ] || [ -z "$DB_USER" ]; then
  echo "Set DB_NAME and DB_USER (and DB_HOST/DB_PASSWORD) for the Postgres target." >&2
  echo "See the header of this script for the full list." >&2
  exit 1
fi

echo "Source SQLite : $SQLITE_PATH"
echo "Target Postgres: user=$DB_USER db=$DB_NAME host=${DB_HOST:-<socket>} port=$DB_PORT"
echo "Dump file      : $DUMP_FILE"
echo
echo "WARNING: the target Postgres database must be EMPTY."
if [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "Continue? [y/N] " reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

# --- 1. Dump from SQLite --------------------------------------------------
# Force the SQLite source regardless of any DB=postgres already exported, and
# use natural keys so rows re-link correctly after the load. contenttypes,
# permissions, admin log entries and old sessions are recreated/regenerable,
# so they are excluded to avoid clashes.
echo "Dumping SQLite data -> $DUMP_FILE"
env -u DB DB_NAME="$SQLITE_PATH" "$PY" manage.py dumpdata \
  --natural-foreign --natural-primary \
  -e contenttypes \
  -e auth.permission \
  -e admin.logentry \
  -e sessions.session \
  --indent 2 --output "$DUMP_FILE"

# --- 2. Build the Postgres schema ----------------------------------------
echo "Applying migrations to Postgres..."
DB=postgres "$PY" manage.py migrate --noinput

# --- 3. Load the data into Postgres ---------------------------------------
echo "Loading data into Postgres..."
DB=postgres "$PY" manage.py loaddata "$DUMP_FILE"

echo
echo "Migration complete."
echo "Next steps:"
echo "  1. Sanity-check the site against Postgres (run with DB=postgres exported)."
echo "  2. Point the systemd units / EnvironmentFile at DB=postgres + DB_* vars."
echo "  3. Once happy, stop using $SQLITE_PATH (keep it as a rollback safety net)."
