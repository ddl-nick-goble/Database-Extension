#!/usr/bin/env bash
# DB App entrypoint. Called by /mnt/code/app.sh when DD_ROLE=postgres|mongo.
#
# Steps:
#   1. lifecycle.boot()  — restore (or init) + start engine + pgweb + cron
#   2. Cache config at /tmp/dd-config.json so the Flask router reads quickly
#   3. Launch the Flask router on $PORT (Domino app port — default 8888)
#
# Path-aware: this script may live at /opt/dd/app.sh (baked into the env
# image) OR at /mnt/code/dbapp/app.sh (legacy in-repo dev iteration). In
# both cases, we cd into the parent of the `dbapp/` Python package so
# `from dbapp.lifecycle import boot` resolves.

set -euo pipefail

: "${PORT:=8888}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "$SCRIPT_DIR/dbapp" ]; then
    DD_MODULE_HOME="$SCRIPT_DIR"                   # baked layout: /opt/dd/{app.sh, dbapp/, snapshotter/}
elif [ -d "$(dirname "$SCRIPT_DIR")/dbapp" ]; then
    DD_MODULE_HOME="$(dirname "$SCRIPT_DIR")"      # repo layout:  /mnt/code/dbapp/app.sh + /mnt/code/dbapp/
else
    echo "[dbapp] ERROR: can't locate dbapp/ from $SCRIPT_DIR" >&2
    exit 2
fi
cd "$DD_MODULE_HOME"

mkdir -p /var/log/dd

# Sanity check: the env image must ship pgweb.
command -v pgweb >/dev/null || { echo "[dbapp] ERROR: pgweb not on PATH — rebuild dd-postgres-app." >&2; exit 1; }

echo "[dbapp] booting sidecars (module home: $DD_MODULE_HOME)…"
python3 -c "
import json, sys
from dbapp.lifecycle import boot
cfg = boot()
open('/tmp/dd-config.json', 'w').write(json.dumps(cfg))
print('[dbapp] config cached at /tmp/dd-config.json')
"

# Teardown snapshot: when Domino sends SIGTERM (App stop / container kill),
# run a final pg_basebackup before letting gunicorn die. lifecycle.boot
# stages /tmp/dd-final-snapshot.sh with all the env vars baked in.
_dd_teardown() {
    if [ -x /tmp/dd-final-snapshot.sh ]; then
        echo "[dbapp] caught signal — running teardown snapshot…" >&2
        bash /tmp/dd-final-snapshot.sh >> /var/log/dd/snapshot.log 2>&1 \
            && echo "[dbapp] teardown snapshot OK" >&2 \
            || echo "[dbapp] teardown snapshot FAILED — letting shutdown proceed" >&2
    fi
    if [ -n "${GUN_PID:-}" ] && kill -0 "$GUN_PID" 2>/dev/null; then
        kill -TERM "$GUN_PID" 2>/dev/null || true
        wait "$GUN_PID" 2>/dev/null || true
    fi
    exit 0
}
trap _dd_teardown TERM INT

echo "[dbapp] launching router on :${PORT}"
if [ "${PORT}" = "8888" ]; then
    # Production: gunicorn with the gthread worker class so flask-sock can
    # serve the /wire WebSocket relay alongside the regular HTTP routes.
    # Run as a child process (NOT exec) so this bash stays around to catch
    # SIGTERM and run the teardown snapshot before gunicorn exits.
    gunicorn \
        --bind "0.0.0.0:${PORT}" \
        --workers 1 \
        --threads 16 \
        --timeout 0 \
        --worker-class gthread \
        --chdir "$DD_MODULE_HOME" \
        dbapp.router:app &
    GUN_PID=$!
    wait "$GUN_PID"
else
    # Dev: Flask's built-in server, fine for one user.
    python3 -m flask --app dbapp/router.py run --host 0.0.0.0 --port "${PORT}" &
    GUN_PID=$!
    wait "$GUN_PID"
fi
