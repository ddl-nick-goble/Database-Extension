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

# Start cron so the snapshotter entry installed by lifecycle.schedule_snapshotter
# actually fires. apt's `cron` package installs it but doesn't start a daemon;
# we run it in the foreground-detached mode (cron -L 15 forks itself).
if command -v cron >/dev/null && ! pgrep -x cron >/dev/null; then
    sudo cron 2>/dev/null || cron 2>/dev/null || \
        echo "[dbapp] WARN: cron daemon not started — snapshots won't fire" >&2
fi

echo "[dbapp] booting sidecars (module home: $DD_MODULE_HOME)…"
python3 -c "
import json, sys
from dbapp.lifecycle import boot
cfg = boot()
open('/tmp/dd-config.json', 'w').write(json.dumps(cfg))
print('[dbapp] config cached at /tmp/dd-config.json')
"

echo "[dbapp] launching router on :${PORT}"
if [ "${PORT}" = "8888" ]; then
    # Production: gunicorn with the gthread worker class so flask-sock can
    # serve the /wire WebSocket relay alongside the regular HTTP routes.
    exec gunicorn \
        --bind "0.0.0.0:${PORT}" \
        --workers 1 \
        --threads 16 \
        --timeout 0 \
        --worker-class gthread \
        --chdir "$DD_MODULE_HOME" \
        dbapp.router:app
else
    # Dev: Flask's built-in server, fine for one user.
    exec python3 -m flask --app dbapp/router.py run --host 0.0.0.0 --port "${PORT}"
fi
