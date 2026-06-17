"""Postgres snapshotter — versioning via Domino dataset snapshots.

One tick:
  1. pg_basebackup → <snapshot_dir>/basebackup/  (live, overwritten)
  2. Capture a versioned Domino dataset snapshot of the backup (see
     _dataset_snapshot.trigger_domino_snapshot — engine-agnostic).

Version history lives in Domino (visible in the dataset UI under "Snapshots"),
not in timestamped directories on disk. Restore reads from the live path on
normal boot. To roll back to an older version, a user manually picks a
Domino snapshot from the UI.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

from _dataset_snapshot import trigger_domino_snapshot

# --------------------------------------------------------------------------
# Config from env (set by lifecycle.schedule_snapshotter)
# --------------------------------------------------------------------------
DB_ID = os.environ.get("DD_DB_ID") or os.environ.get("DOMINO_RUN_ID", "default")
PG_PORT = os.environ.get("DD_PG_PORT", "5432")
PG_USER = os.environ.get("DD_PG_USER", "domino")
PG_PASSWORD = os.environ.get("DD_PG_PASSWORD", "")

_DOMINO_DATASETS_DIR = os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data")
_DOMINO_PROJECT_NAME = os.environ.get("DOMINO_PROJECT_NAME", "default")
SNAPSHOT_ROOT = Path(
    os.environ.get(
        "DD_SNAPSHOT_DIR",
        f"{_DOMINO_DATASETS_DIR}/{_DOMINO_PROJECT_NAME}/db-{DB_ID}",
    )
)
BASEBACKUP_DIR = SNAPSHOT_ROOT / "basebackup"
WAL_DIR = SNAPSHOT_ROOT / "wal"

# Domino API
API_HOST = os.environ.get("DOMINO_API_PROXY") or os.environ.get(
    "DOMINO_API_HOST", "http://localhost:8899",
)
API_KEY = os.environ.get("DOMINO_USER_API_KEY", "")
PROJECT_ID = os.environ.get("DOMINO_PROJECT_ID", "")


def log(msg: str) -> None:
    print(f"[snapshot] {dt.datetime.utcnow().isoformat()}Z {msg}", flush=True)


# --------------------------------------------------------------------------
# pg_basebackup: write to a staging dir, then atomically swap into place so
# the canonical path is never half-written.
# --------------------------------------------------------------------------
def basebackup() -> None:
    staging = SNAPSHOT_ROOT / "basebackup.new"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    env = os.environ.copy()
    env["PGPASSWORD"] = PG_PASSWORD
    subprocess.run(
        [
            "/usr/lib/postgresql/16/bin/pg_basebackup",
            "-h", "127.0.0.1", "-p", PG_PORT, "-U", PG_USER,
            "-D", str(staging),
            "-Ft", "-z", "-Xs", "-P",
        ],
        env=env, check=True,
    )
    # Atomic-ish swap. NFS-mounted datasets honor rename2(), so the window
    # where the canonical path has stale data is sub-millisecond.
    old = SNAPSHOT_ROOT / "basebackup.old"
    if old.exists():
        shutil.rmtree(old)
    if BASEBACKUP_DIR.exists():
        BASEBACKUP_DIR.rename(old)
    staging.rename(BASEBACKUP_DIR)
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------
def main() -> int:
    # Pick up runtime backup path change without requiring container restart.
    _override = Path("/tmp/dd-backup-override.json")
    if _override.exists():
        try:
            import json as _json
            _d = _json.loads(_override.read_text())
            if _d.get("db_id") == DB_ID and _d.get("snapshot_dir"):
                global SNAPSHOT_ROOT, BASEBACKUP_DIR, WAL_DIR
                SNAPSHOT_ROOT = Path(_d["snapshot_dir"])
                BASEBACKUP_DIR = SNAPSHOT_ROOT / "basebackup"
                WAL_DIR = SNAPSHOT_ROOT / "wal"
        except Exception:
            pass

    if not PG_PASSWORD:
        log("DD_PG_PASSWORD not set — refusing to run")
        return 2
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    WAL_DIR.mkdir(parents=True, exist_ok=True)

    log(f"pg_basebackup → {BASEBACKUP_DIR} (staging then swap)")
    try:
        basebackup()
    except subprocess.CalledProcessError as e:
        log(f"pg_basebackup failed (rc={e.returncode}): {e}")
        return 1

    trigger_domino_snapshot(
        api_host=API_HOST, api_key=API_KEY, project_id=PROJECT_ID,
        snapshot_root=SNAPSHOT_ROOT, datasets_dir=_DOMINO_DATASETS_DIR, log=log,
    )
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
