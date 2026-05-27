"""Flask router for a Domino Databases App.

This is the ONLY process bound to port 8888 (the Domino App's exposed port).
Everything else — the engine, the admin UI, ws2tcp — runs internally and is
fronted here. Routes:

  GET  /              → status page (HTML)
  GET  /api/status    → JSON status
  GET  /api/config    → JSON (sanitized — no password)
  WS   /wire          → byte-transparent relay to the local engine port
  *    /admin/*       → reverse-proxy to the engine's admin UI
  GET  /healthz       → engine.health_check()

The engine-specific bits — admin port, connection snippets, health probe —
come from the EngineAdapter that lifecycle.boot() resolved. We re-resolve
it here from cfg["engine"] so the router doesn't need to know any engine
name directly.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template_string, request
from flask_sock import Sock

# --------------------------------------------------------------------------
# Config — dbapp/app.sh writes /tmp/dd-config.json after lifecycle.boot()
# completes. Router fails to start if it's missing rather than guess.
# --------------------------------------------------------------------------
CONFIG_CACHE = Path("/tmp/dd-config.json")
if not CONFIG_CACHE.exists():
    raise RuntimeError(
        f"router: {CONFIG_CACHE} missing — lifecycle.boot() didn't write it. "
        f"Check /var/log/dd/preRun.log."
    )
CFG = json.loads(CONFIG_CACHE.read_text())
ENGINE = CFG["engine"]

# Resolve the engine adapter once. The Python import machinery has
# already populated the registry by side-effect of `import dbapp.engines`.
from dbapp import engines
ADAPTER = engines.get(ENGINE)

# /wire targets either the engine's own port or the pooler (e.g. pgbouncer)
# if lifecycle pushed one into cfg["client_port"].
ENGINE_PORT = int(CFG.get("client_port") or CFG.get("port") or ADAPTER.default_port)
ADMIN_PORT = int(CFG.get("admin_port", 8978))

# Whether this engine has an admin UI launched by lifecycle.
HAS_ADMIN = ADAPTER.admin_ui_spec(CFG) is not None

app = Flask(__name__)
sock = Sock(app)


# --------------------------------------------------------------------------
# Status page
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
    a.btn:disabled, a.btn.disabled { background: #9ca3af; cursor: not-allowed; }
    code { background: #f3f4f6; padding: 1px 6px; border-radius: 3px; font-size: 0.9em; }
    pre.snippet { background: #0f172a; color: #d1d5db; padding: 14px; border-radius: 4px;
                  font-size: 12px; overflow-x: auto; line-height: 1.6; }
    .label { font-size: 12px; color: #6b7280; margin-top: 14px; }
    .note  { font-size: 12px; color: #9ca3af; margin-top: 6px; font-style: italic; }
  </style>
</head><body>
  <h1>{{ cfg.db_id }}</h1>
  <div class="subtitle">{{ adapter.docs_label }} • Domino Databases</div>

  <div class="card">
    <h2>Status</h2>
    <div class="row"><div class="key">Engine</div><div class="val">{{ adapter.docs_label }} ({{ engine }})</div></div>
    <div class="row"><div class="key">Internal port</div><div class="val">{{ engine_port }}</div></div>
    <div class="row"><div class="key">Health</div><div class="val ok">● running</div></div>
  </div>

  <div class="card">
    <h2>Open</h2>
    {% if has_admin %}
    <a class="btn" href="admin/">Open DB Admin →</a>
    {% else %}
    <a class="btn disabled">DB Admin n/a</a>
    {% endif %}
    <a class="btn" href="api/status">JSON status →</a>
    {% if has_admin %}
    <p style="font-size: 13px; color: #4b5563; margin-top: 12px;">
      Admin UI is pre-connected to this DB — no extra login needed.
    </p>
    {% endif %}
  </div>

  <div class="card">
    <h2>Connect from your laptop</h2>
    <p style="font-size: 13px; color: #4b5563; margin-top: 0;">
      Step 1 — open a tunnel. Single-file Python script, zero install.
      Replace <code>$DOMINO_API_KEY</code> with your key from Account Settings.
    </p>
    <pre class="snippet">curl -fsSL https://raw.githubusercontent.com/ddl-nick-goble/Database-Extension/main/client/domino-db-tunnel.py | python3 - \
  --url "{{ cfg.tunnel_url|default('https://apps.<your-domino-host>/apps-internal/<appId>/') }}" \
  --api-key $DOMINO_API_KEY \
  --port {{ engine_port }}</pre>

    <p style="font-size: 13px; color: #4b5563; margin-top: 16px;">
      Step 2 — leave that running. In another terminal:
    </p>
    {% for snip in snippets %}
      <div class="label">{{ snip.label }}</div>
      <pre class="snippet">{{ snip.snippet }}</pre>
      {% if snip.note %}<div class="note">{{ snip.note }}</div>{% endif %}
    {% endfor %}

    {% if not cfg.tunnel_url %}
    <p style="font-size: 12px; color: #ca8a04; margin-top: 12px;">
      Note: this DB was provisioned before the App URL was baked into the config.
      Replace <code>&lt;your-domino-host&gt;</code> and <code>&lt;appId&gt;</code>
      by hand, or re-create the DB to get the snippet auto-filled.
    </p>
    {% endif %}
  </div>
</body></html>
"""


@app.route("/")
def index():
    snippets = ADAPTER.connection_strings(CFG, ENGINE_PORT)
    return render_template_string(
        STATUS_HTML,
        cfg=CFG, adapter=ADAPTER, engine=ENGINE,
        engine_port=ENGINE_PORT, has_admin=HAS_ADMIN,
        snippets=snippets,
    )


@app.route("/api/status")
def api_status():
    return jsonify({
        "engine": ENGINE,
        "engine_label": ADAPTER.docs_label,
        "db_id": CFG["db_id"],
        "internal_port": ENGINE_PORT,
        "admin_at": "/admin/" if HAS_ADMIN else None,
        "wire_at": "/wire",
    })


@app.route("/api/config")
def api_config():
    safe = {k: v for k, v in CFG.items() if k != "password"}
    return jsonify(safe)


# --------------------------------------------------------------------------
# Wire-protocol tunnel (WebSocket ↔ TCP) — engine-agnostic
# --------------------------------------------------------------------------
@sock.route("/wire")
def wire(ws):
    """Byte-transparent relay between an incoming WebSocket and the local
    DB engine's TCP socket. The laptop CLI opens this WS and exposes a
    local TCP listener; psql/mongosh/mysql/redis-cli see what they expect.
    """
    try:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # TCP_NODELAY: small handshake messages (Postgres SCRAM, MySQL
        # caching_sha2_password, Mongo OP_QUERY, Redis pipelined commands)
        # see ~40 ms savings per round-trip without Nagle.
        # SO_KEEPALIVE: detect dead peers so idle WS connections get
        # cleaned up instead of lingering as zombie threads.
        tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
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
# /admin/ — reverse-proxy to the engine's admin UI.
# Each adapter is responsible for making its UI prefix-aware (pgweb has
# --prefix, mongo-express has ME_CONFIG_SITE_BASEURL, redis-commander has
# --url-prefix, phpMyAdmin is configured via PmaAbsoluteUri). We always
# forward the full /admin/<path> upstream.
# --------------------------------------------------------------------------
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def _proxy_to_admin(path: str):
    upstream = f"http://127.0.0.1:{ADMIN_PORT}/admin/{path}"
    headers = {
        k: v for k, v in request.headers
        if k.lower() not in HOP_BY_HOP and k.lower() != "host"
    }
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
        return Response(f"admin upstream error: {e}", status=502)
    resp_headers = [(k, v) for k, v in r.headers.items() if k.lower() not in HOP_BY_HOP]
    return Response(r.iter_content(chunk_size=8192), status=r.status_code, headers=resp_headers)


@app.route("/admin/", defaults={"path": ""},
           methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.route("/admin/<path:path>",
           methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def admin(path):
    if not HAS_ADMIN:
        return Response(
            "<!doctype html><body style='font-family:sans-serif;padding:40px'>"
            f"<h2>No admin UI available for {ADAPTER.docs_label}</h2>"
            "<p>Use the laptop tunnel + a native client from the status page.</p>"
            "</body>",
            mimetype="text/html",
        )
    return _proxy_to_admin(path)


# --------------------------------------------------------------------------
# Health probe — Domino's app-monitor hits /healthz on a schedule. Returning
# 503 surfaces a sick App in the dashboard.
# --------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    try:
        if ADAPTER.health_check(CFG):
            return ("ok", 200)
        return (f"{ENGINE} unhealthy", 503)
    except Exception as e:
        return (f"{ENGINE} unhealthy: {e}", 503)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8888"))
    app.run(host="0.0.0.0", port=port, debug=False)
