#!/usr/bin/env bash
# Domino Databases — root dispatcher.
#
# All apps in this project share /mnt/code/app.sh. We pick the role based
# on DD_ROLE, which each compute environment sets via an ENV line:
#
#   dd-wizard env (or unset)  → wizard  → python app.py
#   dd-postgres-app env       → DB app  → bash dbapp/app.sh
#   dd-mongo-app env          → DB app  → bash dbapp/app.sh
#
# Pattern lifted from MRM-Portal: $PORT, port cleanup, cliSiteConfig URL
# resolution, click-to-open URL print.

set -euo pipefail
cd /mnt/code

export PORT="${PORT:-8888}"
export DD_ROLE="${DD_ROLE:-wizard}"

# ── Kill stale dev processes (NEVER touch port 8888 = prod) ──────────────
if [ "$PORT" != "8888" ]; then
    pkill -9 -f "python app.py" 2>/dev/null || true
    pkill -9 -f "gunicorn .*app:app" 2>/dev/null || true
    pkill -9 -f "dbapp.router:app" 2>/dev/null || true
    fuser -k "$PORT/tcp" 2>/dev/null || true
    for i in 1 2 3 4 5; do
        fuser "$PORT/tcp" >/dev/null 2>&1 || break
        echo "Waiting for port $PORT to free up... ($i/5)"
        sleep 1
    done
    if fuser "$PORT/tcp" >/dev/null 2>&1; then
        echo "ERROR: Port $PORT still in use; force killing"
        fuser -k -9 "$PORT/tcp" 2>/dev/null || true
        sleep 1
    fi
fi

# ── Resolve the real public Domino host via cliSiteConfig ────────────────
_HOST=$(curl -sf http://localhost:8899/cliSiteConfig \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['host'].rstrip('/'))" 2>/dev/null || true)
if [ -n "${_HOST}" ] && [ -z "${DOMINO_PUBLIC_HOST:-}" ]; then
    export DOMINO_PUBLIC_HOST="${_HOST}"
fi

if [ -n "${DOMINO_RUN_ID:-}" ] && [ "$PORT" != "8888" ]; then
    _PATH=$(echo "${DOMINO_RUN_HOST_PATH:-}" | sed 's|/r/|/|g' | sed 's|/$||')
    echo
    echo "  Open: ${_HOST}${_PATH}/proxy/${PORT}/"
    echo
fi

# ── Install deps (idempotent) ────────────────────────────────────────────
pip install -q -r requirements.txt

# ── Dispatch ─────────────────────────────────────────────────────────────
case "$DD_ROLE" in
    postgres|mongo|database)
        echo "DD_ROLE=$DD_ROLE — launching DB app router on :$PORT"
        exec bash /mnt/code/dbapp/app.sh
        ;;
    wizard|"")
        echo "DD_ROLE=wizard — launching Domino Databases wizard on :$PORT"
        if [ "$PORT" = "8888" ]; then
            exec gunicorn --bind "0.0.0.0:${PORT}" --workers 2 --threads 4 --timeout 60 app:app
        else
            exec python app.py
        fi
        ;;
    *)
        echo "ERROR: unknown DD_ROLE=$DD_ROLE" >&2
        exit 2
        ;;
esac
