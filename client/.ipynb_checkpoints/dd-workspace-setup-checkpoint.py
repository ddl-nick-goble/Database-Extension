#!/usr/bin/env python3
"""Domino workspace DB auto-connect setup.

Discovers running database apps in the current Domino project and opens
WebSocket tunnels so users can connect with psql/mongosh/etc directly —
no manual tunnel setup required.

Designed to run as a Domino environment preRunScript:

    python3 /opt/dd/workspace-setup.py

Each tunnel process is started with start_new_session=True so it survives
after this script exits and continues running for the lifetime of the
workspace container.

Shell helpers are written to ~/.bashrc:
    db pg-fin4              → psql -h 127.0.0.1 -p 5432 -U domino -d postgres
    db mongo-analytics      → mongosh 127.0.0.1:27017
    db                      → print available DBs and their connect commands

For programmatic / multi-DB access (e.g. a Python script connecting to several
databases at once), it also writes:
    ~/.dd/connections.json  → {db_name: {engine, host, port, user, dbname, uri}}
    ~/.pgpass               → so psql / psycopg2 authenticate without prompting

    import json, psycopg2
    conns = json.load(open(os.path.expanduser("~/.dd/connections.json")))
    db = {n: psycopg2.connect(c["uri"]) for n, c in conns.items()
          if c["engine"] == "postgres"}
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

# ── Domino env vars (available in any execution / workspace) ─────────────────
_API_PROXY  = os.environ.get("DOMINO_API_PROXY", "http://localhost:8899")
_API_KEY    = os.environ.get("DOMINO_USER_API_KEY", "")
_PROJECT_ID = os.environ.get("DOMINO_PROJECT_ID", "")
_PUBLIC_HOST = os.environ.get("DOMINO_PUBLIC_HOST", "")

# If DOMINO_PUBLIC_HOST isn't set, resolve it from the auth proxy's
# cliSiteConfig endpoint (same method used by app.sh).
if not _PUBLIC_HOST:
    try:
        _cfg_req = Request(f"{_API_PROXY}/cliSiteConfig",
                           headers={"Accept": "application/json"})
        with urlopen(_cfg_req, timeout=5) as _r:
            _PUBLIC_HOST = json.loads(_r.read()).get("host", "").rstrip("/")
    except Exception:
        pass

# Prefer the baked-in copy; fall back to alongside this script (dev workspace).
_TUNNEL_SCRIPT = (
    Path("/opt/dd/domino-db-tunnel.py")
    if Path("/opt/dd/domino-db-tunnel.py").exists()
    else Path(__file__).parent / "domino-db-tunnel.py"
)
_LOG_DIR = Path("/var/log/dd") if Path("/var/log/dd").exists() else Path.home() / ".dd" / "logs"

# engine-name-prefix → (engine_label, base_local_port, connect_template)
_ENGINE_MAP: dict[str, tuple[str, int, str]] = {
    "pg-":    ("postgres", 5432,  "psql -h 127.0.0.1 -p {port} -U domino -d postgres"),
    "mongo-": ("mongo",   27017,  "mongosh 127.0.0.1:{port}"),
    "mysql-": ("mysql",   3306,   "mysql -h 127.0.0.1 -P {port} -u domino -p"),
    "redis-": ("redis",   6379,   "redis-cli -h 127.0.0.1 -p {port}"),
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[dd-connect] {msg}", flush=True)


def _api_get(path: str, params: dict | None = None) -> dict | list:
    url = f"{_API_PROXY}{path}"
    if params:
        url += "?" + urlencode(params)
    req = Request(url, headers={"Accept": "application/json"})
    if _API_KEY:
        req.add_header("X-Domino-Api-Key", _API_KEY)
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _find_running_db_apps() -> list[dict]:
    """Return running DB apps in the current project, annotated with engine info."""
    try:
        data = _api_get("/v4/modelProducts", {"projectId": _PROJECT_ID})
    except Exception as exc:
        _log(f"API error: {exc}")
        return []

    raw = data if isinstance(data, list) else (
        data.get("data") or data.get("items") or data.get("results") or []
    )

    result: list[dict] = []
    for app in raw:
        name = app.get("name", "")
        if str(app.get("status", "")).lower() != "running":
            continue
        for prefix, (engine, base_port, tpl) in _ENGINE_MAP.items():
            if name.startswith(prefix):
                result.append({
                    **app,
                    "_engine":    engine,
                    "_prefix":    prefix,
                    "_base_port": base_port,
                    "_tpl":       tpl,
                })
                break
    return result


def _app_direct_url(app: dict) -> str:
    """Return the apps-internal URL the tunnel connects to."""
    direct = app.get("url", "")
    if direct and direct.startswith("http"):
        return direct.rstrip("/")

    open_path = app.get("openUrl", "")
    if open_path and _PUBLIC_HOST:
        host = _PUBLIC_HOST.rstrip("/")
        if host.startswith("https://") and not host.startswith("https://apps."):
            host = "https://apps." + host[len("https://"):]
        elif host.startswith("http://") and not host.startswith("http://apps."):
            host = "http://apps." + host[len("http://"):]
        return f"{host}{open_path}".rstrip("/")

    running = app.get("runningAppUrl", "")
    if running and running.startswith("http"):
        return running.rstrip("/")
    return ""


def _port_listening(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.3)
        s.close()
        return True
    except OSError:
        return False


def _assign_ports(apps: list[dict]) -> dict[str, int]:
    """Give each app a local port, incrementing past collisions."""
    used: set[int] = set()
    result: dict[str, int] = {}
    for app in apps:
        port = app["_base_port"]
        while port in used:
            port += 1
        used.add(port)
        result[app["name"]] = port
    return result


def _start_tunnel(app_url: str, port: int, app_name: str) -> int | None:
    """Spawn a detached tunnel process; return its PID or None on failure."""
    if not _TUNNEL_SCRIPT.exists():
        _log(f"tunnel script not found at {_TUNNEL_SCRIPT}")
        return None

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _LOG_DIR / f"tunnel-{app_name}.log"

    cmd = [sys.executable, str(_TUNNEL_SCRIPT),
           "--url", app_url,
           "--port", str(port)]
    if _API_KEY:
        cmd += ["--api-key", _API_KEY]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=open(log_file, "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,   # detach so it survives after this script exits
        )
        return proc.pid
    except Exception as exc:
        _log(f"tunnel start failed for {app_name}: {exc}")
        return None


def _write_bashrc(apps: list[dict], port_map: dict[str, int]) -> None:
    """Write/replace the dd-connect block in ~/.bashrc and ensure login shells load it."""
    _MARKER_START = "# >>> domino-db-connect start >>>"
    _MARKER_END   = "# <<< domino-db-connect end <<<"

    lines = [_MARKER_START, "# Auto-generated by dd-workspace-setup.py", ""]

    # One alias per app: db_pg_fin4 → psql ...
    for app in apps:
        name = app["name"]
        port = port_map[name]
        cmd  = app["_tpl"].format(port=port)
        lines.append(f"# {name}")
        lines.append(f"alias db_{name.replace('-', '_')}='{cmd}'")
        lines.append("")

    # Universal dispatcher: db pg-fin4 | db mongo-analytics | db
    lines += [
        "db() {",
        '  case "${1:-}" in',
    ]
    for app in apps:
        name = app["name"]
        port = port_map[name]
        cmd  = app["_tpl"].format(port=port)
        lines.append(f'    "{name}") shift; {cmd} "$@" ;;')
    lines += [
        "    *)",
    ]
    if apps:
        lines.append('      echo "Available databases:"')
        for app in apps:
            name = app["name"]
            port = port_map[name]
            cmd  = app["_tpl"].format(port=port)
            lines.append(f'      echo "  {name:<24} {cmd}"')
    else:
        lines.append('      echo "No databases are currently running in this project."')
        lines.append('      echo "Start a DB app from the Domino Databases wizard, then restart this workspace."')
    lines += [
        "      ;;",
        "  esac",
        "}",
        "",
        _MARKER_END,
    ]

    block = "\n".join(lines) + "\n"

    bashrc = Path.home() / ".bashrc"
    try:
        existing = bashrc.read_text() if bashrc.exists() else ""
        if _MARKER_START in existing and _MARKER_END in existing:
            s = existing.index(_MARKER_START)
            e = existing.index(_MARKER_END) + len(_MARKER_END)
            while e < len(existing) and existing[e] in ("\n", "\r"):
                e += 1
            existing = existing[:s] + existing[e:]
        bashrc.write_text(existing.rstrip("\n") + "\n" + block)
    except Exception as exc:
        _log(f"could not update ~/.bashrc: {exc}")
        return

    # Ensure login shells (bash_profile / profile) also source .bashrc so
    # the db() function is available in all terminal types.
    _SOURCE_SNIPPET = '[ -f ~/.bashrc ] && source ~/.bashrc'
    for profile in (Path.home() / ".bash_profile", Path.home() / ".profile"):
        try:
            text = profile.read_text() if profile.exists() else ""
            if _SOURCE_SNIPPET not in text:
                with open(profile, "a") as f:
                    f.write(f"\n{_SOURCE_SNIPPET}\n")
        except Exception as exc:
            _log(f"could not update {profile}: {exc}")


# ── machine-readable connection manifest + .pgpass ───────────────────────────

def _decode_app_cfg(app: dict) -> dict:
    """Return the per-DB config (user, password, …) the wizard stashed as the
    project env var DD_CFG_<app_id>. Those vars are injected into this workspace
    too (same project), so we can read credentials without any extra API call.
    Empty dict if unavailable (e.g. an older DB)."""
    app_id = app.get("id", "")
    if not app_id:
        return {}
    raw = os.environ.get(f"DD_CFG_{app_id.upper()}", "")
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw).decode())
    except Exception as exc:
        _log(f"  {app.get('name','?')}: could not decode DD_CFG_{app_id}: {exc}")
        return {}


def _conn_details(app: dict, port: int, cfg: dict) -> dict:
    """Build a connection record (host/port/user/dbname/uri) for one DB app.
    Credentials come from cfg; the host is always the local tunnel endpoint."""
    engine = app["_engine"]
    host = "127.0.0.1"
    user = cfg.get("user", "domino")
    pw = cfg.get("password", "")
    qu, qp = quote(user, safe=""), quote(pw, safe="")
    cred = f"{qu}:{qp}@" if pw else (f"{qu}@" if user else "")

    if engine == "postgres":
        dbname, uri = "postgres", f"postgresql://{cred}{host}:{port}/postgres"
    elif engine == "mongo":
        dbname, uri = "admin", f"mongodb://{cred}{host}:{port}/?authSource=admin"
    elif engine == "mysql":
        dbname, uri = "", f"mysql://{cred}{host}:{port}/"
    elif engine == "redis":
        # redis has no username; password (if any) goes in the userinfo slot.
        dbname = "0"
        uri = f"redis://:{qp}@{host}:{port}/0" if pw else f"redis://{host}:{port}/0"
    else:
        dbname, uri = "", ""

    rec = {"engine": engine, "host": host, "port": port, "user": user,
           "dbname": dbname, "uri": uri}
    if pw:
        rec["password"] = pw
    return rec


def _write_connections_manifest(entries: dict) -> Path:
    """Write ~/.dd/connections.json (0600) — the machine-readable map a script
    loads to connect to every DB in the project at once."""
    out = Path.home() / ".dd" / "connections.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(entries, indent=2))
    out.chmod(0o600)
    return out


def _pgpass_escape(s: str) -> str:
    """libpq .pgpass uses ':' and '\\' as metacharacters — backslash-escape them."""
    return s.replace("\\", "\\\\").replace(":", "\\:")


def _write_pgpass(entries: dict) -> Path | None:
    """Write/refresh ~/.pgpass (0600) so psql/psycopg2 to our tunnel ports
    authenticate without a prompt. Preserves any unrelated existing entries;
    only our 127.0.0.1:<tunnel-port> lines are replaced."""
    pg = {c["port"]: c for c in entries.values()
          if c.get("engine") == "postgres" and c.get("password")}
    if not pg:
        return None
    pgpass = Path.home() / ".pgpass"
    our_ports = {str(p) for p in pg}
    kept: list[str] = []
    if pgpass.exists():
        for ln in pgpass.read_text().splitlines():
            parts = ln.split(":")
            if len(parts) >= 2 and parts[0] == "127.0.0.1" and parts[1] in our_ports:
                continue  # drop a stale line we previously wrote for this port
            if ln.strip():
                kept.append(ln)
    for c in pg.values():
        kept.append(":".join([
            "127.0.0.1", str(c["port"]), "*",
            _pgpass_escape(c.get("user", "domino")),
            _pgpass_escape(c["password"]),
        ]))
    pgpass.write_text("\n".join(kept) + "\n")
    pgpass.chmod(0o600)
    return pgpass


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not _PROJECT_ID:
        _log("DOMINO_PROJECT_ID not set — nothing to do")
        return

    _log("discovering running database apps...")
    apps = _find_running_db_apps()

    port_map = _assign_ports(apps) if apps else {}
    connected: list[tuple[str, int]] = []

    if not apps:
        _log("no running database apps found in project — writing stub db() to ~/.bashrc")
    else:
        for app in apps:
            name     = app["name"]
            port     = port_map[name]
            app_url  = _app_direct_url(app)

            if not app_url:
                _log(f"  {name}: no URL available, skipping")
                continue

            if _port_listening(port):
                _log(f"  {name}: port {port} already in use — tunnel likely already running")
                connected.append((name, port))
                continue

            pid = _start_tunnel(app_url, port, name)
            if pid:
                _log(f"  {name} → 127.0.0.1:{port}  (pid {pid})")
                connected.append((name, port))
            else:
                _log(f"  {name}: failed to start tunnel")

    _write_bashrc(apps, port_map)

    # Machine-readable manifest + .pgpass for programmatic / multi-DB access.
    entries = {
        app["name"]: _conn_details(app, port_map[app["name"]], _decode_app_cfg(app))
        for app in apps if app["name"] in port_map
    }
    try:
        manifest = _write_connections_manifest(entries)
        pgpass = _write_pgpass(entries)
    except Exception as exc:
        manifest, pgpass = None, None
        _log(f"could not write connection manifest / .pgpass: {exc}")

    _log("")
    _log("  App                      Port   Connect command")
    _log("  " + "─" * 64)
    for app in apps:
        name = app["name"]
        if name not in port_map:
            continue
        port = port_map[name]
        cmd  = app["_tpl"].format(port=port)
        _log(f"  {name:<26} {port:<6} {cmd}")
    _log("")
    _log("Run  db <app-name>  to connect (source ~/.bashrc first if needed)")
    if manifest:
        _log(f"Programmatic access: {manifest}"
             + (" (+ ~/.pgpass for password-free psql)" if pgpass else ""))
        _log('  python: conns = json.load(open(os.path.expanduser("~/.dd/connections.json")))')

    # Brief pause so tunnel processes have time to bind their ports.
    time.sleep(1.5)


if __name__ == "__main__":
    main()
