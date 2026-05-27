"""Shared test harness — used by every e2e_*.py and the cross-engine tests.

Extracted from e2e_postgres.py so adding a new engine is just a thin test
that uses the engine's own native client (psql / mongosh / mysql / redis-cli)
on top of these helpers.

Convention: every helper is a free function (no test framework), so the
e2e scripts can be run with plain `python3` and still pipe-and-exit cleanly.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import requests

WIZARD = os.environ.get("DD_WIZARD_URL", "http://127.0.0.1:8501")
API_KEY = os.environ.get("DOMINO_USER_API_KEY", "")
PROJECT_NAME = os.environ.get("DOMINO_PROJECT_NAME", "Database-Extension")
DOMINO_DATASETS_DIR = os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data")


def log(prefix: str, msg: str) -> None:
    print(f"[{prefix} {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def step(name: str) -> None:
    print(f"\n=== {name} ===", flush=True)


# --------------------------------------------------------------------------
# Engine-meta lookup — read straight from /api/config so the test mirrors
# what the wizard actually surfaces (no per-engine constants in the tests).
# --------------------------------------------------------------------------
def engine_meta(engine: str) -> dict:
    cfg = requests.get(f"{WIZARD}/api/config", timeout=20).json()
    for e in cfg.get("engines", []):
        if e["name"] == engine:
            return e
    raise SystemExit(
        f"engine {engine!r} not in /api/config.engines — known: "
        f"{[e['name'] for e in cfg.get('engines', [])]}"
    )


def find_env_id(env_name: str) -> str:
    envs = requests.get(f"{WIZARD}/api/environments", timeout=20).json()
    match = next((e for e in envs if e["name"] == env_name), None)
    if not match:
        raise SystemExit(
            f"env {env_name!r} not found. Available: {[e['name'] for e in envs]}"
        )
    return match["id"]


def first_hw_tier_id() -> tuple[str, str]:
    tiers = requests.get(f"{WIZARD}/api/hardware-tiers", timeout=20).json()
    if not tiers:
        raise SystemExit("no hardware tiers visible")
    return tiers[0]["id"], tiers[0].get("name", "?")


# --------------------------------------------------------------------------
# DB lifecycle through the wizard API
# --------------------------------------------------------------------------
def create_db(
    *, engine: str, name: str, env_id: str, hw_id: str,
    password: str, snapshot_interval_min: int = 1,
) -> dict:
    body = {
        "engine": engine,
        "name": name,
        "environmentId": env_id,
        "hardwareTierId": hw_id,
        "password": password,
        "user": "domino",
        "snapshotIntervalMin": snapshot_interval_min,
    }
    r = requests.post(f"{WIZARD}/api/databases", json=body, timeout=60)
    if r.status_code >= 400:
        raise SystemExit(f"create failed {r.status_code}: {r.text[:1000]}")
    # The wizard streams SSE; parse the terminal "result" event.
    # For simplicity in tests, we POST and rely on the wizard to send a
    # final shape inside an event-stream body — locate it.
    for line in r.text.splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            try:
                import json
                obj = json.loads(data)
                if obj.get("id"):
                    return obj
            except Exception:
                continue
    raise SystemExit(f"no terminal result in wizard SSE:\n{r.text[:1000]}")


def poll_until_running(app_id: str, full_name: str, deadline_s: int = 300) -> dict:
    """Wait until the App reaches Running. Tolerates transient Stopped/Failed
    flapping for up to 120s — Domino's Apps API does that on this build."""
    start = time.time()
    last_status = ""
    stopped_first_seen_at: float | None = None
    while time.time() - start < deadline_s:
        dbs = requests.get(f"{WIZARD}/api/databases", timeout=20).json()
        me = next((d for d in dbs["databases"] if d["id"] == app_id), None)
        if not me:
            raise SystemExit(f"app {app_id} disappeared from listing")
        status = me.get("status", "")
        if status != last_status:
            log("poll", f"  status: {status}  (url={me.get('url') or '?'})")
            last_status = status
            if status.lower() not in ("stopped", "failed"):
                stopped_first_seen_at = None
        if status.lower() == "running":
            return me
        if status.lower() in ("failed", "stopped"):
            now = time.time()
            if stopped_first_seen_at is None:
                stopped_first_seen_at = now
            if now - stopped_first_seen_at > 120:
                err = me.get("startError", "")
                raise SystemExit(
                    f"app sat in {status!r} for >120s (startError={err})"
                )
        time.sleep(4)
    raise SystemExit(
        f"timed out after {deadline_s}s waiting for {full_name} to reach Running"
    )


def wait_for_healthz(app_url: str, timeout_s: int = 120) -> None:
    """Domino flips the App's status to Running before the gunicorn worker
    is actually listening. /healthz 200 is the reliable signal."""
    headers = {"X-Domino-Api-Key": API_KEY}
    base = app_url.rstrip("/")
    deadline = time.time() + timeout_s
    last_code = 0
    while time.time() < deadline:
        try:
            r = requests.get(base + "/healthz", headers=headers,
                             timeout=10, allow_redirects=False)
            last_code = r.status_code
            if r.status_code == 200 and r.text.strip().startswith("ok"):
                log("wait", f"  /healthz → 200 ok")
                return
        except requests.RequestException as e:
            last_code = -1
            log("wait", f"  /healthz transient error: {e}")
        time.sleep(3)
    raise SystemExit(
        f"App /healthz never returned 200 within {timeout_s}s (last={last_code})"
    )


def delete_db(app_id: str) -> None:
    try:
        requests.delete(
            f"{WIZARD}/api/databases/{app_id}",
            params={"keep": "0"}, timeout=60,
        )
    except Exception as e:
        log("cleanup", f"  delete error: {e}")


# --------------------------------------------------------------------------
# Laptop tunnel — runs in this workspace, exposes app's /wire as 127.0.0.1:port
# --------------------------------------------------------------------------
def start_tunnel(app_url: str, local_port: int) -> subprocess.Popen:
    log("tunnel", f"  {app_url}/wire → 127.0.0.1:{local_port}")
    cfg_path = Path.home() / ".domino-db" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        f'{{"host":"https://cloud-dogfood.domino.tech","api_key":"{API_KEY}",'
        f'"owner":"{os.environ.get("DOMINO_PROJECT_OWNER", "nick_goble")}",'
        f'"project":"{PROJECT_NAME}"}}'
    )
    cfg_path.chmod(0o600)
    return subprocess.Popen(
        [sys.executable, "/mnt/code/client/domino_db.py", "tunnel",
         app_url, "--local-port", str(local_port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def wait_for_listener(port: int, timeout_s: int = 15) -> None:
    for _ in range(timeout_s * 2):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"tunnel client never bound to 127.0.0.1:{port}")


# --------------------------------------------------------------------------
# Snapshot landing — same shape for every engine, only the marker file
# differs. Caller passes a predicate.
# --------------------------------------------------------------------------
def verify_snapshot(
    full_name: str,
    *,
    expect_file: Callable[[Path], bool],
    timeout_s: int = 120,
) -> Path:
    """Poll the dataset's snapshots dir; succeed when expect_file returns True
    for any candidate. Returns the matching snapshot path."""
    snap_dir = Path(DOMINO_DATASETS_DIR) / PROJECT_NAME / f"db-{full_name}" / "snapshots"
    log("snap", f"  looking under {snap_dir}")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if snap_dir.exists():
            for entry in sorted(snap_dir.iterdir()):
                if entry.name == "latest":
                    continue
                if expect_file(entry):
                    log("snap", f"  found snapshot {entry.name}")
                    return entry
        time.sleep(5)
    raise SystemExit(f"no snapshot appeared at {snap_dir} within {timeout_s}s")


# --------------------------------------------------------------------------
# Cleanup helper used by every e2e script's `finally` clause.
# --------------------------------------------------------------------------
def cleanup(tunnel_proc: subprocess.Popen | None, app_id: str | None,
            success: bool) -> None:
    if tunnel_proc is not None:
        log("cleanup", "stopping tunnel client")
        tunnel_proc.terminate()
        try:
            tunnel_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel_proc.kill()
    keep_on_fail = os.environ.get("DD_E2E_KEEP_ON_FAIL", "1") == "1"
    if app_id and (success or not keep_on_fail):
        log("cleanup", f"deleting app {app_id}")
        delete_db(app_id)
    elif app_id:
        log("cleanup",
            f"app left alive for debugging: {app_id} "
            f"(set DD_E2E_KEEP_ON_FAIL=0 to auto-clean)")
