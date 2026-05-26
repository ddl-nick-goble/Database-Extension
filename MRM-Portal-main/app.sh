#!/usr/bin/env bash
set -euo pipefail

cd /mnt/code

export PORT="${PORT:-8888}"
export GRAPH_REFRESH="${GRAPH_REFRESH:-25}"
export GRAPH_REFRESH_WORKERS="${GRAPH_REFRESH_WORKERS:-64}"

# ── Kill anything holding the port, then wait for socket release ──────
# Never kill on port 8888 (production Domino App port) — only clean up
# stale dev processes on other ports.
if [ "$PORT" != "8888" ]; then
    pkill -9 -f "python app.py" 2>/dev/null || true
    fuser -k "$PORT/tcp" 2>/dev/null || true

    # Wait for port to actually be released
    for i in 1 2 3 4 5; do
        if ! fuser "$PORT/tcp" >/dev/null 2>&1; then
            break
        fi
        echo "Waiting for port $PORT to free up... ($i/5)"
        sleep 1
    done

    # Final check
    if fuser "$PORT/tcp" >/dev/null 2>&1; then
        echo "ERROR: Port $PORT still in use after 5 seconds. Force killing..."
        fuser -k -9 "$PORT/tcp" 2>/dev/null || true
        sleep 1
    fi
fi

# ── Resolve the real public Domino host ───────────────────────────────
_HOST=$(curl -sf http://localhost:8899/cliSiteConfig \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['host'].rstrip('/'))" 2>/dev/null || true)

# Export so shared/config.py picks it up instead of falling back.
if [ -n "${_HOST}" ] && [ -z "${DOMINO_PUBLIC_HOST:-}" ]; then
    export DOMINO_PUBLIC_HOST="${_HOST}"
fi

# ── Print the real public URL ─────────────────────────────────────────
if [ -n "${DOMINO_RUN_ID:-}" ]; then
    _PATH=$(echo "${DOMINO_RUN_HOST_PATH:-}" | sed 's|/r/|/|g' | sed 's|/$||')
    echo ""
    echo "  Open: ${_HOST}${_PATH}/proxy/${PORT}/"
    echo ""
fi

echo "Starting Governance Control Tower on port $PORT..."
python app.py
