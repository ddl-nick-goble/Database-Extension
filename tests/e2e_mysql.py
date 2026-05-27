"""End-to-end test for MySQL on the Domino Databases architecture.

Mirrors tests/e2e_postgres.py shape but uses the mysql CLI through the
WS tunnel. Verifies:
  - wizard creates a MySQL App and it reaches Running
  - /healthz green
  - mysql round-trip via the tunnel (CREATE TABLE / INSERT / SELECT)
  - hourly snapshot lands in the dataset (dump.sql.gz present)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from _dd_helpers import (  # type: ignore[import-not-found]
    log, step, engine_meta, find_env_id, first_hw_tier_id,
    create_db, poll_until_running, wait_for_healthz,
    start_tunnel, wait_for_listener, verify_snapshot, cleanup,
)

DB_NAME = os.environ.get("DD_E2E_DB_NAME", f"e2e-{int(time.time())}")
MYSQL_PASSWORD = "e2e-secret-123"
LOCAL_TUNNEL_PORT = 23306

TUNNEL_PROC = None
APP_ID = None


def mysql(query: str) -> str:
    r = subprocess.run(
        ["mysql", "-h", "127.0.0.1", "-P", str(LOCAL_TUNNEL_PORT),
         "--protocol=TCP",
         "-u", "domino", f"-p{MYSQL_PASSWORD}",
         "-e", query, "-N", "-B"],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise SystemExit(
            f"mysql failed (rc={r.returncode}):\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
        )
    return r.stdout.strip()


def main() -> int:
    global TUNNEL_PROC, APP_ID
    log("e2e", f"DB_NAME={DB_NAME}")

    step("1. discover env + hw tier")
    meta = engine_meta("mysql")
    env_id = meta["envId"] or find_env_id("dd-mysql-app")
    hw_id, hw_name = first_hw_tier_id()
    log("e2e", f"  env={env_id}  hw={hw_name}")

    step("2. POST /api/databases (create + start)")
    app = create_db(
        engine="mysql", name=DB_NAME, env_id=env_id, hw_id=hw_id,
        password=MYSQL_PASSWORD, snapshot_interval_min=1,
    )
    APP_ID = app["id"]
    full_name = f"mysql-{DB_NAME}"
    log("e2e", f"  created id={APP_ID}")

    step("3. poll until Running")
    app = poll_until_running(APP_ID, full_name)
    app_url = app["url"]

    step("4. /healthz green")
    wait_for_healthz(app_url)

    step("5. tunnel + mysql round-trip")
    TUNNEL_PROC = start_tunnel(app_url, LOCAL_TUNNEL_PORT)
    wait_for_listener(LOCAL_TUNNEL_PORT)
    one = mysql("SELECT 1;")
    log("e2e", f"  SELECT 1 → {one!r}")
    if one != "1":
        raise SystemExit(f"unexpected mysql result: {one!r}")
    mysql("CREATE DATABASE IF NOT EXISTS e2e;")
    mysql("CREATE TABLE IF NOT EXISTS e2e.marker (v VARCHAR(64));")
    mysql(f"INSERT INTO e2e.marker VALUES ('hello from e2e {DB_NAME}');")
    row = mysql("SELECT v FROM e2e.marker;")
    log("e2e", f"  SELECT v → {row!r}")
    if "hello from e2e" not in row:
        raise SystemExit(f"unexpected row content: {row!r}")

    step("6. snapshot landed")
    verify_snapshot(
        full_name,
        expect_file=lambda p: (p / "dump.sql.gz").exists(),
    )

    log("e2e", "\nALL GREEN ✓")
    return 0


if __name__ == "__main__":
    success = False
    rc = 1
    try:
        rc = main()
        success = (rc == 0)
    except SystemExit as e:
        log("e2e", f"FAILURE: {e}")
        rc = 1
    except Exception as e:
        log("e2e", f"UNEXPECTED ERROR: {e!r}")
        rc = 2
    finally:
        cleanup(TUNNEL_PROC, APP_ID, success=success)
    sys.exit(rc)
