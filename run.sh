#!/usr/bin/env bash
# Start the Healthchecks web UI and the probe/alert worker together.
#
# Active API probes and automatic probe-result emails run inside the
# "sendalerts" worker process, so it must stay running for checks to be
# probed on schedule. This script runs both processes in one terminal;
# press Ctrl-C to stop both.
set -u
cd "$(dirname "$0")"

PY="${VENV_PYTHON:-/home/muhammad-ans/webapps/.venv/bin/python}"

# Stop leftover instances from earlier runs first. A stale sendalerts worker
# would keep running OLD code and send outdated notification formats:
pkill -f "$PY manage.py sendalerts" 2>/dev/null
pkill -f "$PY manage.py runserver" 2>/dev/null
pkill -f "$PY manage.py housekeeping" 2>/dev/null
sleep 1

"$PY" manage.py sendalerts &
WORKER_PID=$!

# Daily housekeeping: pruning, weekly/monthly report emails, probe history
# cleanup. Runs its first pass immediately, then repeats every 24 hours:
"$PY" manage.py housekeeping --loop &
HOUSEKEEPING_PID=$!

"$PY" manage.py runserver &
WEB_PID=$!

trap 'kill $WORKER_PID $HOUSEKEEPING_PID $WEB_PID 2>/dev/null' INT TERM EXIT
wait
