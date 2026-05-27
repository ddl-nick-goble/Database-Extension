"""End-to-end test for Redis on the Domino Databases architecture.

Mirrors tests/e2e_postgres.py shape but uses redis-cli through the WS
tunnel. Verifies:
  - wizard creates a Redis App and it reaches Running
  - /healthz green
  - redis-cli round-trip via the tunnel (SET / GET / INCR)
  - AOF persistence is enabled (INFO persistence: aof_enabled:1)
  - hourly snapshot lands in the dataset (dump.rdb.gz present)
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
REDIS_PASSWORD = "e2e-secret-123"
LOCAL_TUNNEL_PORT = 26379

TUNNEL_PROC = None
APP_ID = None


def redis_cli(*args: str) -> str:
    r = subprocess.run(
        ["redis-cli", "-h", "127.0.0.1", "-p", str(LOCAL_TUNNEL_PORT),
         "-a", REDIS_PASSWORD, "--no-auth-warning", *args],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        raise SystemExit(
            f"redis-cli failed (rc={r.returncode}):\n{r.stdout}\n{r.stderr}"
        )
    return r.stdout.strip()


def main() -> int:
    global TUNNEL_PROC, APP_ID
    log("e2e", f"DB_NAME={DB_NAME}")

    step("1. discover env + hw tier")
    meta = engine_meta("redis")
    env_id = meta["envId"] or find_env_id("dd-redis-app")
    hw_id, hw_name = first_hw_tier_id()
    log("e2e", f"  env={env_id}  hw={hw_name}")

    step("2. POST /api/databases (create + start)")
    app = create_db(
        engine="redis", name=DB_NAME, env_id=env_id, hw_id=hw_id,
        password=REDIS_PASSWORD, snapshot_interval_min=1,
    )
    APP_ID = app["id"]
    full_name = f"redis-{DB_NAME}"
    log("e2e", f"  created id={APP_ID}")

    step("3. poll until Running")
    app = poll_until_running(APP_ID, full_name)
    app_url = app["url"]

    step("4. /healthz green")
    wait_for_healthz(app_url)

    step("5. tunnel + redis round-trip")
    TUNNEL_PROC = start_tunnel(app_url, LOCAL_TUNNEL_PORT)
    wait_for_listener(LOCAL_TUNNEL_PORT)
    pong = redis_cli("PING")
    log("e2e", f"  PING → {pong!r}")
    if pong != "PONG":
        raise SystemExit(f"unexpected redis result: {pong!r}")
    redis_cli("SET", "e2e:marker", f"hello from e2e {DB_NAME}")
    val = redis_cli("GET", "e2e:marker")
    log("e2e", f"  GET e2e:marker → {val!r}")
    if "hello from e2e" not in val:
        raise SystemExit(f"unexpected value: {val!r}")
    cnt = redis_cli("INCR", "e2e:counter")
    if cnt != "1":
        raise SystemExit(f"INCR did not return 1: {cnt!r}")

    step("6. AOF persistence enabled")
    info = redis_cli("INFO", "persistence")
    if "aof_enabled:1" not in info:
        raise SystemExit(
            f"AOF not enabled — got:\n{info[:400]}"
        )
    log("e2e", "  aof_enabled:1 ✓")

    step("7. snapshot landed")
    verify_snapshot(
        full_name,
        expect_file=lambda p: (p / "dump.rdb.gz").exists(),
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
