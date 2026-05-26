"""Flask router for a Domino Databases App.

This is the ONLY process bound to port 8888 (the Domino App's exposed port).
Everything else — Postgres/Mongo, CloudBeaver, ws2tcp — runs internally and
is fronted here. Routes:

  GET  /              → status page (HTML)
  GET  /api/status    → JSON status
  GET  /api/config    → JSON (sanitized — no password)
  WS   /wire          → byte-transparent relay to the local engine port
  *    /admin/*       → reverse-proxy to CloudBeaver on 127.0.0.1:8978

dbapp/app.sh calls lifecycle.boot() to start sidecars, then launches this.
"""

from __future__ import annotations

import os
import socket
import threading

import requests
from flask import Flask, Response, jsonify, render_template_string, request
from flask_sock import Sock

# Config — loaded once when the router starts (lifecycle.boot writes it
# to /tmp/dd-config.json so we don't re-read the original file).
import json
import sys
from pathlib import Path

CONFIG_CACHE = Path("/tmp/dd-config.json")


def _config() -> dict:
    if CONFIG_CACHE.exists():
        return json.loads(CONFIG_CACHE.read_text())
    # Fallback: re-discover via lifecycle.find_config
    from lifecycle import find_config
    return find_config()


CFG = _config()
ENGINE = CFG["engine"]
ENGINE_PORT = CFG.get("port", 5432 if ENGINE == "postgres" else 27017)
CLOUDBEAVER_PORT = CFG.get("cloudbeaver_port", 8978)

app = Flask(__name__)
sock = Sock(app)


# --------------------------------------------------------------------------
# Status / config endpoints
# --------------------------------------------------------------------------
STATUS_HTML = """<!doctype html>
<html><head>
  <meta charset="utf-8">
  <title>Domino Databases — {{ cfg.db_id }}</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
           background: #fafbfc; color: #1f2937; margin: 0; padding: 40px; max-width: 760px;
           margin-left: auto; margin-right: auto; }
    h1 { font-size: 22px; margin-bottom: 4px; }
    .subtitle { color: #6b7280; margin-bottom: 32px; }
    .card { background: white; border: 1px solid #e4e7eb; border-radius: 6px; padding: 20px 24px; margin-bottom: 16px; }
    .card h2 { font-size: 13px; letter-spacing: 0.06em; text-transform: uppercase; color: #6b7280; margin: 0 0 12px 0; }
    .row { display: flex; padding: 6px 0; border-bottom: 1px solid #f3f4f6; }
    .row:last-child { border-bottom: none; }
    .key { width: 200px; color: #6b7280; font-size: 13px; }
    .val { flex: 1; font-family: ui-monospace, monospace; font-size: 13px; }
    .ok  { color: #16a34a; font-weight: 600; }
    a.btn { display: inline-block; background: #6366f1; color: white; padding: 8px 16px;
            border-radius: 4px; text-decoration: none; font-size: 13px; font-weight: 500;
            margin-right: 8px; }
    a.btn:hover { background: #4f46e5; }
    code { background: #f3f4f6; padding: 1px 6px; border-radius: 3px; font-size: 0.9em; }
  </style>
</head><body>
  <h1>{{ cfg.db_id }}</h1>
  <div class="subtitle">{{ engine }} • Domino Databases</div>

  <div class="card">
    <h2>Status</h2>
    <div class="row"><div class="key">Engine</div><div class="val">{{ engine }}</div></div>
    <div class="row"><div class="key">Internal port</div><div class="val">{{ engine_port }}</div></div>
    <div class="row"><div class="key">Health</div><div class="val ok">● running</div></div>
  </div>

  <div class="card">
    <h2>Open</h2>
    <a class="btn" href="admin/">DB Explorer (CloudBeaver) →</a>
    <a class="btn" href="api/status">JSON status →</a>
  </div>

  <div class="card">
    <h2>Connect from your laptop</h2>
    <p style="font-size: 13px; color: #4b5563; margin-top: 0;">Open a tunnel; <code>psql</code> / <code>mongosh</code> / JDBC / ODBC against <code>localhost</code>:</p>
    <pre style="background: #0f172a; color: #d1d5db; padding: 14px; border-radius: 4px; font-size: 12px; overflow-x: auto; line-height: 1.6;">
domino-db tunnel --app-url {{ request.url_root }} --local-port {{ engine_port }}
{% if engine == 'postgres' -%}
psql "host=127.0.0.1 port={{ engine_port }} user={{ cfg.user|default('domino') }} dbname=postgres"
{%- else -%}
mongosh "mongodb://{{ cfg.user|default('domino') }}@127.0.0.1:{{ engine_port }}/admin"
{%- endif %}</pre>
  </div>
</body></html>
"""


@app.route("/")
def index():
    return render_template_string(STATUS_HTML, cfg=CFG, engine=ENGINE, engine_port=ENGINE_PORT)


@app.route("/api/status")
def api_status():
    return jsonify({
        "engine": ENGINE,
        "db_id": CFG["db_id"],
        "internal_port": ENGINE_PORT,
        "cloudbeaver_at": "/admin/",
        "wire_at": "/wire",
    })


@app.route("/api/config")
def api_config():
    safe = {k: v for k, v in CFG.items() if k != "password"}
    return jsonify(safe)


# --------------------------------------------------------------------------
# Wire-protocol tunnel (WebSocket ↔ TCP)
# --------------------------------------------------------------------------
@sock.route("/wire")
def wire(ws):
    """Byte-transparent relay between an incoming WebSocket and the local
    DB engine's TCP socket. The laptop CLI (`domino-db tunnel`) opens this
    WS and exposes a local TCP listener; psql/mongosh see what they expect.
    """
    try:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.connect(("127.0.0.1", ENGINE_PORT))
        tcp.settimeout(None)
    except OSError as e:
        ws.close(reason=f"engine unreachable: {e}")
        return

    stop = threading.Event()

    def tcp_to_ws():
        try:
            while not stop.is_set():
                chunk = tcp.recv(65536)
                if not chunk:
                    return
                ws.send(chunk)
        except Exception:
            return
        finally:
            stop.set()
            try: ws.close()
            except Exception: pass

    t = threading.Thread(target=tcp_to_ws, daemon=True)
    t.start()
    try:
        while not stop.is_set():
            msg = ws.receive()
            if msg is None:
                break
            if isinstance(msg, str):
                msg = msg.encode()
            tcp.sendall(msg)
    except Exception:
        pass
    finally:
        stop.set()
        try: tcp.close()
        except Exception: pass


# --------------------------------------------------------------------------
# Reverse proxy to CloudBeaver at /admin/
# --------------------------------------------------------------------------
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def _proxy_to_cloudbeaver(path: str):
    upstream = f"http://127.0.0.1:{CLOUDBEAVER_PORT}/admin/{path}"
    headers = {k: v for k, v in request.headers if k.lower() not in HOP_BY_HOP and k.lower() != "host"}
    try:
        r = requests.request(
            method=request.method,
            url=upstream,
            headers=headers,
            data=request.get_data(),
            params=request.args,
            cookies=request.cookies,
            allow_redirects=False,
            stream=True,
            timeout=30,
        )
    except requests.RequestException as e:
        return Response(f"upstream error: {e}", status=502)

    resp_headers = [(k, v) for k, v in r.headers.items() if k.lower() not in HOP_BY_HOP]
    return Response(r.iter_content(chunk_size=8192), status=r.status_code, headers=resp_headers)


@app.route("/admin/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.route("/admin/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def admin(path):
    return _proxy_to_cloudbeaver(path)


# --------------------------------------------------------------------------
# Health (for Domino app health probe)
# --------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    try:
        s = socket.create_connection(("127.0.0.1", ENGINE_PORT), timeout=1)
        s.close()
        return ("ok", 200)
    except Exception as e:
        return (f"engine unreachable: {e}", 503)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8888"))
    app.run(host="0.0.0.0", port=port, debug=False)
