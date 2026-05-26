#!/usr/bin/env bash
# DB App entrypoint. Called by /mnt/code/app.sh when DD_ROLE=postgres|mongo.
#
# Steps:
#   1. lifecycle.boot()  — restore (or init) + start engine + CloudBeaver + cron
#   2. Cache config at /tmp/dd-config.json so the Flask router reads quickly
#   3. Launch the Flask router on $PORT (Domino app port — default 8888)

set -euo pipefail
cd /mnt/code

: "${PORT:=8888}"

mkdir -p /var/log/dd

echo "[dbapp] booting sidecars…"
python3 -c "
import json, sys
from dbapp.lifecycle import boot
cfg = boot()
open('/tmp/dd-config.json', 'w').write(json.dumps(cfg))
print('[dbapp] config cached at /tmp/dd-config.json')
"

echo "[dbapp] launching router on :${PORT}"
if [ "${PORT}" = "8888" ]; then
    # Production: gunicorn with the geventwebsocket worker so /wire WS works.
    # flask-sock + gunicorn requires the gevent worker class.
    exec gunicorn \
        --bind "0.0.0.0:${PORT}" \
        --workers 1 \
        --threads 16 \
        --timeout 0 \
        --worker-class gthread \
        dbapp.router:app
else
    # Dev: Flask's built-in server, fine for one user.
    exec python3 -m flask --app dbapp/router.py run --host 0.0.0.0 --port "${PORT}"
fi
