"""End-to-end test for MongoDB on the Domino Databases architecture.

Mirrors tests/e2e_postgres.py shape but uses mongosh through the WS tunnel.
Verifies:
  - wizard creates a Mongo App and it reaches Running
  - /healthz green
  - mongosh round-trip via the tunnel (insertOne + findOne)
  - hourly snapshot lands in the dataset (dump.bson or dump dir present)
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
MONGO_PASSWORD = "e2e-secret-123"
LOCAL_TUNNEL_PORT = 27018

TUNNEL_PROC = None
APP_ID = None


def mongosh(uri: str, script: str) -> str:
    r = subprocess.run(
        ["mongosh", "--quiet", uri, "--eval", script],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise SystemExit(f"mongosh failed (rc={r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def main() -> int:
    global TUNNEL_PROC, APP_ID
    log("e2e", f"DB_NAME={DB_NAME}")

    step("1. discover env + hw tier")
    meta = engine_meta("mongo")
    env_id = meta["envId"] or find_env_id("dd-mongo-app")
    hw_id, hw_name = first_hw_tier_id()
    log("e2e", f"  env={env_id}  hw={hw_name}")

    step("2. POST /api/databases (create + start)")
    app = create_db(
        engine="mongo", name=DB_NAME, env_id=env_id, hw_id=hw_id,
        password=MONGO_PASSWORD, snapshot_interval_min=1,
    )
    APP_ID = app["id"]
    full_name = f"mongo-{DB_NAME}"
    log("e2e", f"  created id={APP_ID}")

    step("3. poll until Running")
    app = poll_until_running(APP_ID, full_name)
    app_url = app["url"]

    step("4. /healthz green")
    wait_for_healthz(app_url)

    step("5. tunnel + mongosh round-trip")
    TUNNEL_PROC = start_tunnel(app_url, LOCAL_TUNNEL_PORT)
    wait_for_listener(LOCAL_TUNNEL_PORT)
    uri = (
        f"mongodb://domino:{MONGO_PASSWORD}@127.0.0.1:{LOCAL_TUNNEL_PORT}"
        "/admin?directConnection=true"
    )
    mongosh(uri, 'db.getSiblingDB("e2e").marker.insertOne({hello:"e2e ' + DB_NAME + '"})')
    out = mongosh(uri, 'JSON.stringify(db.getSiblingDB("e2e").marker.findOne())')
    log("e2e", f"  findOne → {out!r}")
    if "hello" not in out:
        raise SystemExit(f"unexpected mongosh result: {out!r}")

    step("6. snapshot landed")
    verify_snapshot(
        full_name,
        expect_file=lambda p: any(p.rglob("*.bson*")),
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
