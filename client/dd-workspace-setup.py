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
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
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
        lines.append(f'    "{name}") {cmd} ;;')
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

    # Brief pause so tunnel processes have time to bind their ports.
    time.sleep(1.5)


if __name__ == "__main__":
    main()
