"""End-to-end test for the Domino Databases App architecture.

Creates a Postgres database via the wizard API → waits for the App to boot →
opens a WebSocket tunnel from this workspace to the App's /wire endpoint
with Domino auth → connects psql through the tunnel → runs a query →
verifies snapshot lands in the dataset → cleans up.

Run with `python3 /mnt/code/tests/e2e_postgres.py`. The wizard must already
be running on http://127.0.0.1:8501.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

WIZARD = os.environ.get("DD_WIZARD_URL", "http://127.0.0.1:8501")
API_KEY = os.environ["DOMINO_USER_API_KEY"]
PROJECT_NAME = os.environ.get("DOMINO_PROJECT_NAME", "Database-Extension")
DOMINO_DATASETS_DIR = os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data")

# Unique-ish name so we don't collide with prior test runs.
DB_NAME = os.environ.get("DD_E2E_DB_NAME", f"e2e-{int(time.time())}")
PG_PASSWORD = "e2e-secret-123"
LOCAL_TUNNEL_PORT = 25432

TUNNEL_PROC: subprocess.Popen | None = None
APP_ID: str | None = None


def log(msg: str) -> None:
    print(f"[e2e {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def step(name: str) -> None:
    print(f"\n=== {name} ===", flush=True)


# --------------------------------------------------------------------------
# 1. Catalog discovery
# --------------------------------------------------------------------------
def find_env_id(name: str) -> str:
    envs = requests.get(f"{WIZARD}/api/environments", timeout=20).json()
    match = next((e for e in envs if e["name"] == name), None)
    if not match:
        raise SystemExit(f"env {name!r} not found. Available: {[e['name'] for e in envs]}")
    return match["id"]


def first_hw_tier_id() -> tuple[str, str]:
    tiers = requests.get(f"{WIZARD}/api/hardware-tiers", timeout=20).json()
    if not tiers:
        raise SystemExit("no hardware tiers visible")
    return tiers[0]["id"], tiers[0].get("name", "?")


# --------------------------------------------------------------------------
# 2. Create + start the DB App
# --------------------------------------------------------------------------
def create_db(env_id: str, hw_id: str) -> dict:
    body = {
        "engine": "postgres",
        "name": DB_NAME,
        "environmentId": env_id,
        "hardwareTierId": hw_id,
        "password": PG_PASSWORD,
        "user": "domino",
        "snapshotIntervalMin": 1,  # fast snapshot cycle for the test
    }
    r = requests.post(f"{WIZARD}/api/databases", json=body, timeout=60)
    if r.status_code >= 400:
        raise SystemExit(f"create failed {r.status_code}: {r.text[:1000]}")
    return r.json()


def poll_until_running(app_id: str, deadline_s: int = 300) -> dict:
    """Wait until the App reaches Running.

    On this Domino build, transient 'Stopped' shows up during deployment
    *before* the container actually starts. Don't treat it as terminal —
    Domino re-enters Queued/Pending shortly after. Only declare failure
    after we've seen Stopped/Failed persist past the warmup window OR
    the deadline expires.
    """
    full_name = f"pg-{DB_NAME}"
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
            log(f"  status: {status}  (url={me.get('url') or '?'})")
            last_status = status
            if status.lower() not in ("stopped", "failed"):
                stopped_first_seen_at = None
        if status.lower() == "running":
            return me
        if status.lower() in ("failed", "stopped"):
            now = time.time()
            if stopped_first_seen_at is None:
                stopped_first_seen_at = now
            # Tolerate transient Stopped/Failed for up to 120s — Domino's
            # Apps API regularly flaps to Stopped briefly between Queued and
            # Pending on this build. Real failures sit in Stopped indefinitely.
            if now - stopped_first_seen_at > 120:
                err = me.get("startError", "")
                raise SystemExit(f"app sat in {status!r} for >60s (startError={err})")
        time.sleep(4)
    raise SystemExit(f"timed out after {deadline_s}s waiting for {full_name} to reach Running")


# --------------------------------------------------------------------------
# 3. Verify HTTP endpoints on the running App
# --------------------------------------------------------------------------
def verify_http(app_url: str) -> None:
    headers = {"X-Domino-Api-Key": API_KEY}
    base = app_url.rstrip("/")
    # Domino flips the App's status to Running before the gunicorn worker
    # is actually listening on 8888. The reverse proxy returns 502 in that
    # window. Wait for /healthz to return 200 before declaring success.
    log("  waiting for /healthz to return 200...")
    deadline = time.time() + 120
    last_code = 0
    while time.time() < deadline:
        try:
            r = requests.get(base + "/healthz", headers=headers, timeout=10, allow_redirects=False)
            last_code = r.status_code
            if r.status_code == 200 and r.text.strip().startswith("ok"):
                log(f"  /healthz → 200 ok")
                break
        except requests.RequestException as e:
            last_code = -1
            log(f"  /healthz transient error: {e}")
        time.sleep(3)
    else:
        raise SystemExit(f"App /healthz never returned 200 within 120s (last={last_code})")
    for path in ("/api/status",):
        r = requests.get(base + path, headers=headers, timeout=20, allow_redirects=False)
        snippet = r.text[:120].replace("\n", " ")
        log(f"  GET {path} → {r.status_code}  {snippet}")
        if r.status_code >= 400:
            raise SystemExit(f"App {path} returned {r.status_code}")


# --------------------------------------------------------------------------
# 4. WS tunnel from this workspace
# --------------------------------------------------------------------------
def start_tunnel(app_url: str) -> subprocess.Popen:
    log(f"  starting tunnel client: {app_url}/wire → localhost:{LOCAL_TUNNEL_PORT}")
    # Pre-populate the laptop config so domino_db.py has the API key.
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
         app_url, "--local-port", str(LOCAL_TUNNEL_PORT)],
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


def psql_query(query: str) -> str:
    env = {**os.environ, "PGPASSWORD": PG_PASSWORD}
    r = subprocess.run(
        ["psql",
         "-h", "127.0.0.1", "-p", str(LOCAL_TUNNEL_PORT),
         "-U", "domino", "-d", "postgres",
         "-c", query, "-t", "-A"],
        env=env, capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        raise SystemExit(f"psql failed (rc={r.returncode}):\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}")
    return r.stdout.strip()


# --------------------------------------------------------------------------
# 5. Verify snapshot landed
# --------------------------------------------------------------------------
def verify_snapshot_path() -> None:
    # The wizard prefixes user-supplied names with "pg-", so the dataset
    # subdir matches the App's full name (pg-<DB_NAME>).
    full_name = f"pg-{DB_NAME}"
    snap_dir = Path(DOMINO_DATASETS_DIR) / PROJECT_NAME / f"db-{full_name}" / "snapshots"
    log(f"  looking at {snap_dir}")
    deadline = time.time() + 90  # snapshotIntervalMin=1, so up to ~90s
    while time.time() < deadline:
        if snap_dir.exists():
            entries = sorted(snap_dir.iterdir())
            populated = [
                p for p in entries
                if (p / "basebackup" / "base.tar.gz").exists()
            ]
            if populated:
                latest = populated[-1]
                size = (latest / "basebackup" / "base.tar.gz").stat().st_size
                log(f"  found snapshot {latest.name} (base.tar.gz = {size} bytes)")
                return
        time.sleep(5)
    raise SystemExit(f"no snapshot appeared at {snap_dir} within 90s")


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------
def cleanup(success: bool = False) -> None:
    global TUNNEL_PROC, APP_ID
    if TUNNEL_PROC is not None:
        log("stopping tunnel client")
        TUNNEL_PROC.terminate()
        try:
            TUNNEL_PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            TUNNEL_PROC.kill()
    keep_on_fail = os.environ.get("DD_E2E_KEEP_ON_FAIL", "1") == "1"
    if APP_ID and (success or not keep_on_fail):
        log(f"stopping + deleting app {APP_ID}")
        try:
            requests.delete(
                f"{WIZARD}/api/databases/{APP_ID}",
                params={"keep": "0"},
                timeout=60,
            )
        except Exception as e:
            log(f"  cleanup error: {e}")
    elif APP_ID:
        log(f"app left alive for debugging: {APP_ID} (set DD_E2E_KEEP_ON_FAIL=0 to auto-clean)")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    global TUNNEL_PROC, APP_ID
    log(f"DB_NAME={DB_NAME} PROJECT={PROJECT_NAME}")

    step("1. discover env + hw tier")
    env_id = find_env_id("dd-postgres-app")
    hw_id, hw_name = first_hw_tier_id()
    log(f"  env=dd-postgres-app id={env_id}")
    log(f"  hw={hw_name} id={hw_id}")

    step("2. POST /api/databases  (create + start)")
    app = create_db(env_id, hw_id)
    APP_ID = app["id"]
    log(f"  created id={APP_ID} initial status={app.get('status')}")

    step("3. poll until Running")
    app = poll_until_running(APP_ID)
    app_url = app["url"]
    log(f"  app_url={app_url}")

    step("4. verify HTTP endpoints respond")
    verify_http(app_url)

    step("5. open WS tunnel + psql round-trip")
    TUNNEL_PROC = start_tunnel(app_url)
    wait_for_listener(LOCAL_TUNNEL_PORT)
    one = psql_query("SELECT 1;")
    log(f"  SELECT 1 → {one!r}")
    if one != "1":
        raise SystemExit(f"unexpected psql result: {one!r}")
    psql_query("CREATE TABLE e2e_marker (v text);")
    psql_query(f"INSERT INTO e2e_marker VALUES ('hello from e2e {DB_NAME}');")
    row = psql_query("SELECT v FROM e2e_marker;")
    log(f"  SELECT v FROM e2e_marker → {row!r}")
    if "hello from e2e" not in row:
        raise SystemExit(f"unexpected row content: {row!r}")

    step("6. verify snapshot landed in dataset")
    verify_snapshot_path()

    log("\nALL GREEN ✓")
    return 0


if __name__ == "__main__":
    success = False
    try:
        rc = main()
        success = (rc == 0)
    except SystemExit as e:
        log(f"FAILURE: {e}")
        rc = 1
    except Exception as e:
        log(f"UNEXPECTED ERROR: {e!r}")
        rc = 2
    finally:
        cleanup(success=success)
    sys.exit(rc)
