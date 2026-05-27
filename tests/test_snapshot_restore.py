"""Snapshot/restore roundtrip test — parametrized over all four engines.

For each engine:
  1. Provision the App, wait until Running
  2. Connect via tunnel, write a known marker
  3. Force a snapshot to land in the dataset
  4. Delete the App (which also stops the engine container)
  5. Provision a NEW App with the SAME db_id — lifecycle's restore_or_init
     path should pick up the dataset snapshot
  6. Connect via tunnel again, verify the marker survived

This is the test that catches the failure mode banks care about:
"could we actually restore from a backup if the App container vanished."

Usage:
  python3 tests/test_snapshot_restore.py             # all four engines
  python3 tests/test_snapshot_restore.py --only mysql
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from _dd_helpers import (  # type: ignore[import-not-found]
    log, step, engine_meta, find_env_id, first_hw_tier_id,
    create_db, poll_until_running, wait_for_healthz, delete_db,
    start_tunnel, wait_for_listener, cleanup,
)


ALL_ENGINES = ["postgres", "mongo", "mysql", "redis"]

# Per-engine probe — given (port, password, marker), write + read it.
def _probe_postgres(port: int, password: str, marker: str, *, read_only: bool) -> str:
    env = {**os.environ, "PGPASSWORD": password}
    def psql(q: str) -> str:
        r = subprocess.run(
            ["psql", "-h", "127.0.0.1", "-p", str(port),
             "-U", "domino", "-d", "postgres", "-c", q, "-t", "-A"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise SystemExit(f"psql: {r.stderr}")
        return r.stdout.strip()
    if not read_only:
        psql("CREATE TABLE IF NOT EXISTS sr_marker (v text);")
        psql(f"INSERT INTO sr_marker VALUES ('{marker}');")
    return psql("SELECT v FROM sr_marker LIMIT 1;")


def _probe_mongo(port: int, password: str, marker: str, *, read_only: bool) -> str:
    uri = f"mongodb://domino:{password}@127.0.0.1:{port}/admin?directConnection=true"
    def mongosh(script: str) -> str:
        r = subprocess.run(
            ["mongosh", "--quiet", uri, "--eval", script],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise SystemExit(f"mongosh: {r.stderr}")
        return r.stdout.strip()
    if not read_only:
        mongosh(f'db.getSiblingDB("sr").marker.insertOne({{v:"{marker}"}})')
    return mongosh('JSON.stringify(db.getSiblingDB("sr").marker.findOne()?.v)')


def _probe_mysql(port: int, password: str, marker: str, *, read_only: bool) -> str:
    def mysql(q: str) -> str:
        r = subprocess.run(
            ["mysql", "-h", "127.0.0.1", "-P", str(port), "--protocol=TCP",
             "-u", "domino", f"-p{password}", "-e", q, "-N", "-B"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise SystemExit(f"mysql: {r.stderr}")
        return r.stdout.strip()
    if not read_only:
        mysql("CREATE DATABASE IF NOT EXISTS sr;")
        mysql("CREATE TABLE IF NOT EXISTS sr.marker (v VARCHAR(64));")
        mysql(f"INSERT INTO sr.marker VALUES ('{marker}');")
    return mysql("SELECT v FROM sr.marker LIMIT 1;")


def _probe_redis(port: int, password: str, marker: str, *, read_only: bool) -> str:
    def cli(*args: str) -> str:
        r = subprocess.run(
            ["redis-cli", "-h", "127.0.0.1", "-p", str(port),
             "-a", password, "--no-auth-warning", *args],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            raise SystemExit(f"redis-cli: {r.stderr}")
        return r.stdout.strip()
    if not read_only:
        cli("SET", "sr:marker", marker)
    return cli("GET", "sr:marker")


PROBES = {
    "postgres": _probe_postgres,
    "mongo":    _probe_mongo,
    "mysql":    _probe_mysql,
    "redis":    _probe_redis,
}


LOCAL_PORTS = {
    "postgres": 35432, "mongo": 37017, "mysql": 33306, "redis": 36379,
}

ENV_NAMES = {
    "postgres": "dd-postgres-app",
    "mongo":    "dd-mongo-app",
    "mysql":    "dd-mysql-app",
    "redis":    "dd-redis-app",
}


def _wait_for_snapshot(full_name: str, marker_check, timeout_s: int = 180) -> None:
    """Poll the dataset snapshots dir until at least one snapshot satisfies
    marker_check. Different engines lay out the dump differently, so the
    caller passes a predicate."""
    from pathlib import Path
    base = Path(os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data"))
    project = os.environ.get("DOMINO_PROJECT_NAME", "Database-Extension")
    snap_dir = base / project / f"db-{full_name}" / "snapshots"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if snap_dir.exists():
            for entry in sorted(snap_dir.iterdir()):
                if entry.name == "latest":
                    continue
                if marker_check(entry):
                    log("snap", f"  snapshot ready: {entry.name}")
                    return
        time.sleep(5)
    raise SystemExit(f"no snapshot under {snap_dir} within {timeout_s}s")


def _snapshot_check(engine: str):
    """Returns a callable(p) → bool that decides whether a snapshot dir is
    'complete' for this engine."""
    def for_postgres(p):
        return (p / "basebackup" / "base.tar.gz").exists()
    def for_mongo(p):
        return any(p.rglob("*.bson*"))
    def for_mysql(p):
        return (p / "dump.sql.gz").exists()
    def for_redis(p):
        return (p / "dump.rdb.gz").exists()
    return {
        "postgres": for_postgres,
        "mongo":    for_mongo,
        "mysql":    for_mysql,
        "redis":    for_redis,
    }[engine]


def run_engine(engine: str) -> None:
    step(f"=== snapshot/restore: {engine} ===")
    password = f"sr-{engine}-secret"
    name = f"sr-{engine}-{int(time.time())}"
    full_name = f"{engine_meta(engine)['appPrefix']}{name}"
    local_port = LOCAL_PORTS[engine]
    probe = PROBES[engine]
    snapshot_pred = _snapshot_check(engine)

    meta = engine_meta(engine)
    env_id = meta["envId"] or find_env_id(ENV_NAMES[engine])
    hw_id, _ = first_hw_tier_id()

    # --- round 1: write + snapshot ---
    app = create_db(engine=engine, name=name, env_id=env_id, hw_id=hw_id,
                    password=password, snapshot_interval_min=1)
    app1_id = app["id"]
    try:
        app = poll_until_running(app1_id, full_name)
        wait_for_healthz(app["url"])
        tunnel = start_tunnel(app["url"], local_port)
        try:
            wait_for_listener(local_port)
            marker = f"snapshot-restore-{engine}-{int(time.time())}"
            probe(local_port, password, marker, read_only=False)
            log("sr", f"  wrote marker {marker}")
        finally:
            tunnel.terminate()
            try: tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired: tunnel.kill()
        _wait_for_snapshot(full_name, snapshot_pred)
    finally:
        # Always tear down round-1's App so round 2 starts from the
        # dataset, not a hot data dir.
        delete_db(app1_id)

    # --- round 2: new App, same db_id, expect restore ---
    log("sr", "  waiting 20s for Domino to settle after delete")
    time.sleep(20)

    app = create_db(engine=engine, name=name, env_id=env_id, hw_id=hw_id,
                    password=password, snapshot_interval_min=60)
    app2_id = app["id"]
    try:
        app = poll_until_running(app2_id, full_name)
        wait_for_healthz(app["url"])
        tunnel = start_tunnel(app["url"], local_port)
        try:
            wait_for_listener(local_port)
            got = probe(local_port, password, "", read_only=True)
            log("sr", f"  read-back marker = {got!r}")
            if marker not in got:
                raise SystemExit(
                    f"{engine}: marker did NOT survive restart — "
                    f"got {got!r}, expected substring {marker!r}"
                )
        finally:
            tunnel.terminate()
            try: tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired: tunnel.kill()
    finally:
        delete_db(app2_id)
    log("sr", f"  ✓ {engine} survived snapshot/restore roundtrip\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=ALL_ENGINES,
                        help="run roundtrip for a single engine only")
    args = parser.parse_args()

    targets = [args.only] if args.only else ALL_ENGINES
    failures: list[str] = []
    for engine in targets:
        try:
            run_engine(engine)
        except SystemExit as e:
            log("sr", f"FAILURE ({engine}): {e}")
            failures.append(engine)
        except Exception as e:
            log("sr", f"UNEXPECTED ({engine}): {e!r}")
            failures.append(engine)

    if failures:
        log("sr", f"\nFAILED engines: {failures}")
        return 1
    log("sr", "\nALL ENGINES SURVIVED SNAPSHOT/RESTORE ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
