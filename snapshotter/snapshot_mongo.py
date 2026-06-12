"""MongoDB snapshotter — runs on a fixed interval inside the DB App.

One tick:
  1. mongodump --oplog --gzip into <snapshot_dir>/snapshots.new/dump
  2. Atomic-ish rename to <snapshot_dir>/snapshots/<ts>
  3. Update <snapshot_dir>/snapshots/latest symlink (used by restore)
  4. POST /v4/datasetrw/snapshot to capture a versioned dataset snapshot

Layout mirrors snapshot_postgres.py — both engines write to
$DOMINO_DATASETS_DIR/<project>/db-<id>/, so the wizard + lifecycle code
can use one set of helpers.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

# --------------------------------------------------------------------------
# Config from env (set by lifecycle.schedule_snapshotter)
# --------------------------------------------------------------------------
DB_ID = os.environ.get("DD_DB_ID") or os.environ.get("DOMINO_RUN_ID", "default")
MONGO_PORT = os.environ.get("DD_MONGO_PORT", "27017")
MONGO_USER = os.environ.get("DD_MONGO_USER", "domino")
MONGO_PASSWORD = os.environ.get("DD_MONGO_PASSWORD", "")

_DOMINO_DATASETS_DIR = os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data")
_DOMINO_PROJECT_NAME = os.environ.get("DOMINO_PROJECT_NAME", "default")
SNAPSHOT_ROOT = Path(
    os.environ.get(
        "DD_SNAPSHOT_DIR",
        f"{_DOMINO_DATASETS_DIR}/{_DOMINO_PROJECT_NAME}/db-{DB_ID}",
    )
)
SNAPSHOTS_DIR = SNAPSHOT_ROOT / "snapshots"

DATASET_RELPATH = f"db-{DB_ID}"

API_HOST = os.environ.get("DOMINO_API_PROXY") or os.environ.get(
    "DOMINO_API_HOST", "http://localhost:8899",
)
API_KEY = os.environ.get("DOMINO_USER_API_KEY", "")
PROJECT_ID = os.environ.get("DOMINO_PROJECT_ID", "")


def log(msg: str) -> None:
    print(f"[snapshot] {dt.datetime.utcnow().isoformat()}Z {msg}", flush=True)


def mongodump(staging: Path) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    # --oplog requires the source to be a replSet primary; the Mongo
    # adapter starts mongod as a single-node rs0 specifically for this.
    cmd = [
        "mongodump",
        f"--host=127.0.0.1:{MONGO_PORT}",
        f"--username={MONGO_USER}",
        f"--password={MONGO_PASSWORD}",
        "--authenticationDatabase=admin",
        "--oplog",
        "--gzip",
        f"--out={staging}",
    ]
    log(f"running mongodump → {staging}")
    subprocess.run(cmd, check=True)


def swap_into_place(staging: Path, ts: str) -> Path:
    """Move staging → snapshots/<ts>, point snapshots/latest at it.

    `latest` is a symlink (not a hardlink) so the lifecycle adapter's
    restore code can resolve a stable path.
    """
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    final = SNAPSHOTS_DIR / ts
    if final.exists():
        shutil.rmtree(final, ignore_errors=True)
    staging.rename(final)

    latest_link = SNAPSHOTS_DIR / "latest"
    tmp_link = SNAPSHOTS_DIR / "latest.new"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(ts)  # relative target — works under bind-mounts
    tmp_link.rename(latest_link)
    return final


def trigger_domino_snapshot() -> None:
    if not (API_HOST and API_KEY and PROJECT_ID):
        log("no API credentials / project id — skipping Domino snapshot")
        return
    try:
        with httpx.Client(
            base_url=API_HOST,
            headers={"X-Domino-Api-Key": API_KEY},
            timeout=30,
        ) as c:
            # Same projectIdsToInclude quirk as snapshot_postgres.py.
            r = c.get(
                "/api/datasetrw/v2/datasets",
                params={"projectIdsToInclude": PROJECT_ID},
            )
            r.raise_for_status()
            ds_id = None
            for wrapped in r.json().get("datasets", []):
                ds = wrapped.get("dataset", {})
                if ds.get("projectId") == PROJECT_ID:
                    ds_id = ds.get("id")
                    break
            if not ds_id:
                log(f"no dataset found for project {PROJECT_ID}")
                return
            body = {"datasetId": ds_id, "relativeFilePaths": [DATASET_RELPATH]}
            r = c.post("/v4/datasetrw/snapshot", json=body)
            if r.status_code == 200:
                snap = r.json()
                log(
                    f"Domino snapshot created id={snap.get('id')} "
                    f"version={snap.get('version')}"
                )
            elif r.status_code == 400 and "already in progress" in r.text:
                log("Domino snapshot already in progress — will catch the next tick")
            else:
                log(f"Domino snapshot API {r.status_code}: {r.text[:300]}")
    except Exception as e:
        log(f"Domino snapshot trigger failed: {e}")


def main() -> int:
    # Pick up runtime backup path change without requiring container restart.
    _override = Path("/tmp/dd-backup-override.json")
    if _override.exists():
        try:
            import json as _json
            _d = _json.loads(_override.read_text())
            if _d.get("db_id") == DB_ID and _d.get("snapshot_dir"):
                global SNAPSHOT_ROOT, SNAPSHOTS_DIR, DATASET_RELPATH
                SNAPSHOT_ROOT = Path(_d["snapshot_dir"])
                SNAPSHOTS_DIR = SNAPSHOT_ROOT / "snapshots"
                DATASET_RELPATH = f"db-{DB_ID}"
        except Exception:
            pass

    if not MONGO_PASSWORD:
        log("DD_MONGO_PASSWORD not set — refusing to run")
        return 2
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    staging = SNAPSHOT_ROOT / f"mongodump.new.{ts}"

    try:
        mongodump(staging)
    except subprocess.CalledProcessError as e:
        log(f"mongodump failed (rc={e.returncode})")
        shutil.rmtree(staging, ignore_errors=True)
        return 1

    final = swap_into_place(staging, ts)
    log(f"snapshot ready at {final}")
    trigger_domino_snapshot()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
