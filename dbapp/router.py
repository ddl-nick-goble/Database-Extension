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

import datetime
import json
import os
import socket
import subprocess as _sp
import sys
import threading
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template_string, request, send_from_directory
from flask_sock import Sock
from dbapp import lifecycle

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
# Mutable — updated in-place when the user reconfigures backup via the UI.
_cfg_lock = threading.Lock()
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
  <link rel="icon" type="image/svg+xml" href="img/favicon.svg">
  <link rel="icon" href="img/favicon.ico" sizes="any">
  <link rel="icon" type="image/png" sizes="32x32" href="img/icon-32.png">
  <link rel="apple-touch-icon" href="img/apple-touch-icon.png">
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
                  font-size: 12px; overflow-x: auto; line-height: 1.6; margin: 0; }
    .label { font-size: 12px; color: #6b7280; margin-top: 14px; }
    .note  { font-size: 12px; color: #9ca3af; margin-top: 6px; font-style: italic; }
    .snippet-wrap { position: relative; margin-top: 4px; }
    .copy-btn {
      position: absolute; top: 8px; right: 8px;
      background: rgba(255,255,255,0.1); color: #d1d5db; border: 1px solid rgba(255,255,255,0.2);
      border-radius: 4px; padding: 3px 9px; font-size: 11px; cursor: pointer; transition: background 0.15s;
    }
    .copy-btn:hover { background: rgba(255,255,255,0.2); }
    .copy-btn.copied { background: #16a34a; border-color: #16a34a; color: white; }
    .tip-box {
      background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px;
      padding: 14px 16px; margin-bottom: 4px;
    }
    .tip-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
    .badge-rec {
      background: #16a34a; color: white; font-size: 10px; font-weight: 700;
      letter-spacing: 0.06em; padding: 2px 8px; border-radius: 99px; white-space: nowrap;
    }
    .tip-title { font-size: 13px; font-weight: 600; color: #166534; }
    .tip-body { font-size: 13px; color: #374151; margin: 0 0 10px 0; }
    .step-row { display: flex; gap: 12px; margin-top: 14px; }
    .step-num {
      flex-shrink: 0; width: 22px; height: 22px; border-radius: 50%;
      background: #16a34a; color: white; font-size: 12px; font-weight: 700;
      display: flex; align-items: center; justify-content: center; margin-top: 1px;
    }
    .step-heading { font-size: 13px; font-weight: 600; color: #166534; margin-bottom: 6px; }
    .opt-block {
      border: 1px solid #e4e7eb; border-radius: 6px; padding: 14px 16px;
      margin-top: 10px;
    }
    .opt-recommended { border-color: #bbf7d0; background: #f0fdf4; }
    .opt-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
    .opt-num {
      flex-shrink: 0; width: 22px; height: 22px; border-radius: 50%;
      background: #6366f1; color: white; font-size: 11px; font-weight: 700;
      display: flex; align-items: center; justify-content: center;
    }
    .opt-recommended .opt-num { background: #16a34a; }
    .opt-title { font-size: 13px; font-weight: 600; color: #111827; }
    .opt-recommended .opt-title { color: #166534; }
    .opt-desc { font-size: 13px; color: #4b5563; margin: 0 0 8px 0; }
    .shared-connect {
      margin-top: 14px; padding-top: 14px;
      border-top: 1px solid #e4e7eb;
    }
    .shared-connect-label {
      font-size: 12px; font-weight: 600; letter-spacing: 0.04em;
      text-transform: uppercase; color: #6b7280; margin-bottom: 8px;
    }
  </style>
</head><body>
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
    <svg width="64" height="64" viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="flex-shrink:0;">
      <path d="M26,46 V80 a34,18 0 0 0 68,0 V46 a34,18 0 0 1 -68,0 z" fill="#C24E1B"/>
      <path d="M26,63 a34,18 0 0 0 68,0" fill="none" stroke="#ffffff" stroke-width="5" stroke-linecap="round"/>
      <ellipse cx="60" cy="46" rx="34" ry="18" fill="#E8642A"/>
      <g transform="translate(60 46) rotate(-6) scale(1 0.55) scale(0.0833333) translate(-300 -300)" fill="#ffffff">
        <path d="M280.19 142.036C282.913 150.121 288.605 156.721 296.196 160.516C300.733 162.743 305.6 163.898 310.468 163.898C313.851 163.898 317.315 163.321 320.616 162.248L470.852 112.17C487.599 106.642 496.592 88.4924 491.064 71.7447C485.536 54.997 467.303 46.0044 450.636 51.532L300.403 101.61C283.738 107.137 274.663 125.288 280.19 142.036Z"/>
        <path d="M318.056 481.439C321.851 473.769 322.511 465.186 319.788 457.017C314.261 440.354 296.111 431.278 279.363 436.806L129.129 486.883C121.043 489.608 114.443 495.299 110.648 502.89C106.853 510.481 106.193 519.144 108.916 527.308C113.371 540.674 125.828 549.173 139.194 549.173C142.576 549.173 145.959 548.68 149.259 547.525L299.493 497.446C307.578 494.721 314.178 489.03 317.973 481.439H318.056Z"/>
        <path d="M174.588 202.183C179.125 204.493 183.992 205.565 188.778 205.565C200.493 205.565 211.795 199.13 217.405 187.91L288.191 46.2558C291.986 38.6657 292.646 30.0031 289.924 21.8355C287.201 13.7504 281.509 7.15028 273.918 3.35523C258.161 -4.48236 238.938 1.87022 231.101 17.6279L160.315 159.282C152.477 175.04 158.83 194.263 174.588 202.1V202.183Z"/>
        <path d="M425.308 396.871C417.718 393.076 408.973 392.416 400.888 395.139C392.803 397.861 386.203 403.554 382.408 411.144L311.622 552.797C307.826 560.388 307.166 569.05 309.889 577.219C312.612 585.388 318.304 591.903 325.894 595.699C330.432 597.925 335.217 599.08 340.167 599.08C343.549 599.08 347.015 598.502 350.314 597.431C358.4 594.707 365 589.016 368.795 581.424L439.581 439.772C443.375 432.182 444.036 423.519 441.313 415.351C438.59 407.266 432.898 400.666 425.308 396.871Z"/>
        <path d="M102.067 299.128C106.522 312.493 118.98 320.991 132.428 320.991C135.728 320.991 139.193 320.496 142.493 319.341C159.158 313.813 168.233 295.663 162.706 278.915L112.628 128.681C107.1 112.016 88.9499 102.858 72.2022 108.468C55.5369 113.996 46.4619 132.146 51.9896 148.894L102.067 299.128Z"/>
        <path d="M497.827 299.945C492.299 283.197 474.145 274.205 457.398 279.732C449.313 282.455 442.714 288.147 438.919 295.738C435.124 303.328 434.464 311.99 437.186 320.158L487.264 470.392C491.721 483.758 504.179 492.257 517.541 492.257C520.926 492.257 524.308 491.759 527.609 490.604C544.273 485.076 553.35 466.927 547.822 450.179L497.743 299.945H497.827Z"/>
        <path d="M174.01 442.593C177.392 442.593 180.857 442.015 184.157 440.94C192.242 438.219 198.843 432.527 202.637 424.936C206.432 417.347 207.092 408.684 204.37 400.516C201.648 392.431 195.955 385.831 188.365 382.036L46.7104 311.25C30.9528 303.413 11.7301 309.765 3.8925 325.523C0.0974548 333.195 -0.562554 341.775 2.15998 349.943C4.88251 358.028 10.5751 364.628 18.1652 368.423L159.82 439.209C164.357 441.438 169.225 442.593 174.092 442.593H174.01Z"/>
        <path d="M597.816 249.22C595.096 241.136 589.405 234.535 581.814 230.74L440.159 159.954C424.401 152.117 405.178 158.47 397.341 174.227C393.546 181.9 392.886 190.48 395.608 198.648C398.331 206.732 404.023 213.333 411.614 217.128L553.266 287.914C557.806 290.224 562.673 291.296 567.456 291.296C579.174 291.296 590.477 284.861 596.084 273.641C599.88 266.051 600.541 257.388 597.816 249.22Z"/>
      </g>
    </svg>
    <h1>{{ cfg.db_id }}</h1>
  </div>
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
    <a class="btn" href="admin/">Open DB Admin ↗</a>
    {% else %}
    <a class="btn disabled">DB Admin n/a</a>
    {% endif %}
    {% if has_admin %}
    <p style="font-size: 13px; color: #4b5563; margin-top: 12px;">
      Admin UI is pre-connected to this DB — no extra login needed.
    </p>
    {% endif %}
    <p style="margin-top: 12px;"><a href="api/status" style="font-size: 12px; color: #9ca3af;">JSON status</a></p>
  </div>

  <div class="card">
    <h2>Connect</h2>

    <!-- Option 1 -->
    <div class="opt-block opt-recommended">
      <div class="opt-header">
        <span class="opt-num">1</span>
        <span class="opt-title">DSE + DB environment</span>
        <span class="badge-rec">RECOMMENDED</span>
      </div>
      <p class="opt-desc">
        Start a Domino workspace with the <code>dd-dse-db</code> environment.
        The pre-run script auto-connects all databases in this project — no tunnel command needed.
      </p>
      <div class="snippet-wrap">
        <pre class="snippet">db {{ cfg.db_id }}</pre>
        <button class="copy-btn" onclick="copySnippet(this)">Copy</button>
      </div>
      <div class="note">Run <code>db</code> with no args to list all databases. Run <code>source ~/.bashrc</code> if the alias isn't loaded yet.</div>
    </div>

    <!-- Option 2 -->
    <div class="opt-block">
      <div class="opt-header">
        <span class="opt-num">2</span>
        <span class="opt-title">From a Domino workspace</span>
      </div>
      <p class="opt-desc"><code>$DOMINO_API_KEY</code> is already set in your environment. Run this tunnel in one terminal:</p>
      <div class="snippet-wrap">
        <pre class="snippet">python3 /mnt/code/client/domino-db-tunnel.py \
  --url "{{ cfg.tunnel_url|default('https://apps.<domino-host>/apps-internal/<appId>/') }}" \
  --api-key $DOMINO_API_KEY \
  --port {{ engine_port }}</pre>
        <button class="copy-btn" onclick="copySnippet(this)">Copy</button>
      </div>
    </div>

    <!-- Option 3 -->
    <div class="opt-block">
      <div class="opt-header">
        <span class="opt-num">3</span>
        <span class="opt-title">From your laptop</span>
      </div>
      <p class="opt-desc">Get your API key from Domino → Account Settings → API Keys, then run:</p>
      <div class="snippet-wrap">
        <pre class="snippet">export DOMINO_API_KEY=&lt;your-key&gt;</pre>
        <button class="copy-btn" onclick="copySnippet(this)">Copy</button>
      </div>
      <div class="snippet-wrap" style="margin-top:6px;">
        <pre class="snippet">curl -fsSL https://raw.githubusercontent.com/ddl-nick-goble/Database-Extension/main/client/domino-db-tunnel.py | python3 - \
  --url "{{ cfg.tunnel_url|default('https://apps.<domino-host>/apps-internal/<appId>/') }}" \
  --api-key $DOMINO_API_KEY \
  --port {{ engine_port }}</pre>
        <button class="copy-btn" onclick="copySnippet(this)">Copy</button>
      </div>
      {% if not cfg.tunnel_url %}
      <div class="note" style="color:#ca8a04;">Replace <code>&lt;domino-host&gt;</code> and <code>&lt;appId&gt;</code> — or re-create this DB to get the URL auto-filled.</div>
      {% endif %}
    </div>

    <!-- Shared step 2 for options 2 + 3 -->
    <div class="shared-connect">
      <div class="shared-connect-label">Then connect <span style="color:#9ca3af;font-weight:400;">(options 2 &amp; 3 — leave the tunnel running)</span></div>
      {% for snip in snippets %}
        <div class="label">{{ snip.label }}</div>
        <div class="snippet-wrap">
          <pre class="snippet">{{ snip.snippet }}</pre>
          <button class="copy-btn" onclick="copySnippet(this)">Copy</button>
        </div>
        {% if snip.note %}<div class="note">{{ snip.note }}</div>{% endif %}
      {% endfor %}
    </div>
  </div>
  <div class="card" id="backup-card">
    <h2>Backup</h2>
    <div id="backup-status-rows"></div>
    <div style="margin-top:16px;">
      <div style="display:flex;gap:12px;margin-bottom:12px;">
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer;">
          <input type="radio" name="backup-mode" value="path" checked> Use existing dataset path
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer;">
          <input type="radio" name="backup-mode" value="create"> Create new Domino dataset
        </label>
      </div>
      <div id="backup-path-input">
        <input id="backup-path" type="text" placeholder="/mnt/data/my-dataset"
          style="width:100%;box-sizing:border-box;font-family:monospace;font-size:13px;
                 padding:8px 10px;border:1px solid #d1d5db;border-radius:4px;margin-bottom:8px;">
        <div style="font-size:12px;color:#6b7280;">Path to a mounted dataset directory. Backups are written to a <code>db-{{ cfg.db_id }}</code> subdirectory within it.</div>
      </div>
      <div id="backup-create-input" style="display:none;">
        <input id="backup-ds-name" type="text" placeholder="my-db-backups"
          style="width:100%;box-sizing:border-box;font-size:13px;
                 padding:8px 10px;border:1px solid #d1d5db;border-radius:4px;margin-bottom:8px;">
        <div style="font-size:12px;color:#6b7280;">Creates a new Domino dataset in this project. The app needs to be restarted once after creation for the dataset to be mounted.</div>
      </div>
      <button onclick="configureBackup()" style="background:#6366f1;color:white;border:none;
        padding:8px 16px;border-radius:4px;font-size:13px;font-weight:500;cursor:pointer;">
        Save backup location
      </button>
      <button onclick="snapshotNow()" style="background:white;color:#374151;border:1px solid #d1d5db;
        padding:8px 16px;border-radius:4px;font-size:13px;font-weight:500;cursor:pointer;margin-left:8px;">
        Snapshot now
      </button>
    </div>
    <div id="backup-msg" style="margin-top:10px;font-size:13px;"></div>
  </div>
  <script>
  function copySnippet(btn) {
    var pre = btn.previousElementSibling;
    var text = pre.textContent.trim();
    function markCopied() {
      btn.textContent = 'Copied!'; btn.classList.add('copied');
      setTimeout(function() { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1800);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(markCopied).catch(function() { fallbackCopy(text, markCopied); });
    } else {
      fallbackCopy(text, markCopied);
    }
  }
  function fallbackCopy(text, cb) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); cb(); } catch(e) {}
    document.body.removeChild(ta);
  }
  (function() {
    // Toggle path vs create inputs
    document.querySelectorAll('input[name="backup-mode"]').forEach(r => {
      r.addEventListener('change', function() {
        document.getElementById('backup-path-input').style.display = this.value === 'path' ? '' : 'none';
        document.getElementById('backup-create-input').style.display = this.value === 'create' ? '' : 'none';
      });
    });

    function setMsg(msg, color) {
      var el = document.getElementById('backup-msg');
      el.style.color = color || '#374151';
      el.textContent = msg;
    }

    function renderStatus(data) {
      var rows = document.getElementById('backup-status-rows');
      var snapshotDir = data.snapshot_dir || '(not configured)';
      var lastSnap = data.last_snapshot || '—';
      var lastStatus = data.last_status || '—';
      rows.innerHTML =
        '<div class="row"><div class="key">Backup location</div><div class="val">' + snapshotDir + '</div></div>' +
        '<div class="row"><div class="key">Last snapshot</div><div class="val">' + lastSnap + '</div></div>' +
        '<div class="row"><div class="key">Last status</div><div class="val ' + (lastStatus === 'ok' ? 'ok' : '') + '">' + lastStatus + '</div></div>';
      if (data.snapshot_dir) {
        document.getElementById('backup-path').value = data.snapshot_dir.replace(/[/]db-[^/]+$/, '');
      }
    }

    function loadStatus() {
      fetch('api/backup/status').then(r => r.json()).then(renderStatus).catch(function(e) {
        document.getElementById('backup-status-rows').innerHTML = '<div class="row"><div class="key">Status</div><div class="val" style="color:#ca8a04">Could not load backup status</div></div>';
      });
    }

    window.configureBackup = function() {
      var mode = document.querySelector('input[name="backup-mode"]:checked').value;
      var body = {mode: mode};
      if (mode === 'path') {
        var p = document.getElementById('backup-path').value.trim();
        if (!p) { setMsg('Enter a dataset path.', '#ca8a04'); return; }
        body.path = p;
      } else {
        var n = document.getElementById('backup-ds-name').value.trim();
        if (!n) { setMsg('Enter a dataset name.', '#ca8a04'); return; }
        body.name = n;
      }
      setMsg('Saving…', '#6b7280');
      fetch('api/backup/configure', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      }).then(r => r.json()).then(function(data) {
        if (data.error) { setMsg('Error: ' + data.error, '#dc2626'); return; }
        if (data.status === 'needs_restart') {
          setMsg('Dataset created at ' + data.mount_path + '. Restart this app for it to be mounted, then set the path above.', '#ca8a04');
        } else {
          setMsg('Saved. Backups will write to ' + data.snapshot_dir, '#16a34a');
          loadStatus();
        }
      }).catch(function(e) { setMsg('Request failed: ' + e, '#dc2626'); });
    };

    window.snapshotNow = function() {
      setMsg('Running snapshot…', '#6b7280');
      fetch('api/backup/snapshot', {method: 'POST'})
        .then(r => r.json())
        .then(function(d) {
          setMsg(d.error ? 'Error: ' + d.error + (d.detail ? ' — ' + d.detail.trim().split('\n').pop() : '') : 'Snapshot done: ' + (d.detail || 'ok'), d.error ? '#dc2626' : '#16a34a');
          loadStatus();
        })
        .catch(function(e) { setMsg('Request failed: ' + e, '#dc2626'); });
    };

    loadStatus();
  })();
  </script>
</body></html>
"""


_STATIC_IMG = Path(__file__).parent.parent / "static" / "img"


@app.route("/img/<path:filename>")
def serve_img(filename):
    return send_from_directory(_STATIC_IMG, filename)


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
# Big pipes: 1 MiB kernel socket buffers + 256 KiB userspace read chunks.
# Overridable via env for ops tuning without a code change.
_WIRE_SOCK_BUF = int(os.environ.get("DD_WIRE_SOCK_BUF", str(1 << 20)))
_WIRE_CHUNK = int(os.environ.get("DD_WIRE_CHUNK", str(256 * 1024)))


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
        # Big pipes: widen the kernel socket buffers so bulk transfers
        # (Redis pipelines / large GETs, Postgres COPY, mongodump) aren't
        # throttled by a small default window. Best-effort — the kernel
        # clamps to net.core.{r,w}mem_max, so a failure here is non-fatal.
        for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
            try:
                tcp.setsockopt(socket.SOL_SOCKET, opt, _WIRE_SOCK_BUF)
            except OSError:
                pass
        tcp.connect(("127.0.0.1", ENGINE_PORT))
        tcp.settimeout(None)
    except OSError as e:
        ws.close(reason=f"engine unreachable: {e}")
        return

    stop = threading.Event()

    def tcp_to_ws():
        try:
            while not stop.is_set():
                chunk = tcp.recv(_WIRE_CHUNK)
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


@app.route("/api/backup/status")
def api_backup_status():
    snap_dir = str(lifecycle.snapshot_path(CFG))
    last_snap, last_status = _last_snapshot_info(snap_dir)
    return jsonify({
        "snapshot_dir": snap_dir,
        "last_snapshot": last_snap,
        "last_status": last_status,
    })


@app.route("/api/backup/configure", methods=["POST"])
def api_backup_configure():
    body = request.get_json(force=True) or {}
    mode = body.get("mode", "path")
    datasets_dir = os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data")

    if mode == "create":
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "dataset name is required"}), 400
        project_id = os.environ.get("DOMINO_PROJECT_ID", "")
        try:
            if "/mnt/code" not in sys.path:
                sys.path.insert(0, "/mnt/code")
            import domino_api as _dapi
            _dapi.create_dataset(name, project_id)
        except Exception as e:
            return jsonify({"error": f"Dataset creation failed: {e}"}), 502
        mount_path = Path(datasets_dir) / name
        if mount_path.exists():
            # Already mounted — configure immediately.
            snap_dir = mount_path / f"db-{CFG['db_id']}"
            _apply_backup_config(snap_dir)
            return jsonify({"status": "ok", "snapshot_dir": str(snap_dir)})
        return jsonify({
            "status": "needs_restart",
            "mount_path": str(mount_path),
            "detail": f"Dataset '{name}' created. Restart the app and then set path to {mount_path}.",
        })

    # mode == "path"
    raw_path = (body.get("path") or "").strip()
    if not raw_path:
        return jsonify({"error": "path is required"}), 400
    base = Path(raw_path)
    if not base.exists():
        return jsonify({"error": f"Path does not exist: {base}. Is the dataset mounted?"}), 400
    # Test writability.
    test_file = base / ".dd_write_test"
    try:
        test_file.write_text("ok")
        test_file.unlink()
    except OSError as e:
        return jsonify({"error": f"Path not writable: {e}"}), 400
    snap_dir = base / f"db-{CFG['db_id']}"
    _apply_backup_config(snap_dir)
    return jsonify({"status": "ok", "snapshot_dir": str(snap_dir)})


@app.route("/api/backup/snapshot", methods=["POST"])
def api_backup_snapshot():
    """Trigger an immediate snapshot for this DB engine."""
    from dbapp import engines as _eng
    adapter = _eng.get(ENGINE)
    script_name = adapter.snapshot_script_name()
    script = None
    for candidate in (f"/mnt/code/snapshotter/{script_name}", f"/opt/dd/snapshotter/{script_name}"):
        if Path(candidate).exists():
            script = candidate
            break
    if not script:
        return jsonify({"error": f"Snapshotter script not found: {script_name}"}), 500

    snap_dir = lifecycle.snapshot_path(CFG)
    env = os.environ.copy()
    env.update({"DD_DB_ID": CFG["db_id"], "DD_SNAPSHOT_DIR": str(snap_dir)})
    env.update(adapter.snapshot_env(CFG))
    try:
        result = _sp.run(
            ["python3", script], env=env, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            return jsonify({"error": "Snapshot failed", "detail": result.stdout[-1000:] + result.stderr[-500:]}), 500
        last_line = (result.stdout.strip().splitlines() or ["done"])[-1]
        return jsonify({"status": "ok", "detail": last_line})
    except _sp.TimeoutExpired:
        return jsonify({"error": "Snapshot timed out after 300s"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _apply_backup_config(snap_dir: Path) -> None:
    """Update in-memory config, persist to disk, and update the project env var."""
    with _cfg_lock:
        CFG["snapshot_dir"] = str(snap_dir)
    lifecycle.save_backup_config(CFG, snap_dir)
    # Push the new path to the Domino project env var so it survives restarts.
    db_id = CFG.get("db_id", "")
    project_id = os.environ.get("DOMINO_PROJECT_ID", "")
    api_key = os.environ.get("DOMINO_USER_API_KEY", "")
    api_proxy = os.environ.get("DOMINO_API_PROXY", "http://localhost:8899")
    if db_id and project_id and api_key:
        snap_var = f"DD_SNAPSHOT_{db_id.replace('-', '_').upper()}"
        try:
            requests.post(
                f"{api_proxy}/v4/projects/{project_id}/environmentVariables",
                json={"name": snap_var, "value": str(snap_dir)},
                headers={"X-Domino-Api-Key": api_key},
                timeout=10,
            )
        except Exception:
            pass


def _last_snapshot_info(snap_dir: str) -> tuple[str, str]:
    """Parse the last line of _diag/snapshot.out for time and status."""
    try:
        log_file = Path(snap_dir) / "_diag" / "snapshot.out"
        if not log_file.exists():
            return "—", "—"
        lines = log_file.read_text().strip().splitlines()
        # Look for a timestamp + "done" or "failed" line.
        for line in reversed(lines):
            if "done" in line.lower():
                return line[:40], "ok"
            if "fail" in line.lower() or "error" in line.lower():
                return line[:40], "failed"
        return lines[-1][:40] if lines else "—", "—"
    except Exception:
        return "—", "—"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8888"))
    app.run(host="0.0.0.0", port=port, debug=False)
