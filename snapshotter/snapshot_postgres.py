"""Postgres snapshotter — versioning via Domino dataset snapshots.

One tick:
  1. pg_basebackup → /mnt/data/<project>/db-<id>/basebackup/  (live, overwritten)
  2. POST /v4/datasetrw/snapshot to capture a versioned point-in-time copy
     of the db-<id>/ subtree in the project's default dataset

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

import httpx

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

# Path within the dataset (relative, no leading slash) — what we ask Domino
# to snapshot. The dataset is mounted at $DOMINO_DATASETS_DIR/<project>/.
DATASET_RELPATH = f"db-{DB_ID}"

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
# Domino dataset snapshot — the actual versioning mechanism
# --------------------------------------------------------------------------
def _find_dataset_id(client: httpx.Client) -> str | None:
    """Return the dataset ID of the project's default dataset.

    /api/datasetrw/v2/datasets?projectId=... is IGNORED on this build —
    use ?projectIdsToInclude=... (verified empirically). Some builds also
    return cross-project results regardless, so we still filter by
    projectId client-side.
    """
    r = client.get(
        "/api/datasetrw/v2/datasets",
        params={"projectIdsToInclude": PROJECT_ID},
    )
    r.raise_for_status()
    for wrapped in r.json().get("datasets", []):
        ds = wrapped.get("dataset", {})
        if ds.get("projectId") == PROJECT_ID:
            return ds.get("id")
    return None


def trigger_domino_snapshot() -> None:
    """POST /v4/datasetrw/snapshot for the db-<id>/ subtree. Idempotent —
    skips silently if another snapshot is in flight on the same dataset.
    """
    if not (API_HOST and API_KEY and PROJECT_ID):
        log("no API credentials / project id — skipping Domino snapshot")
        return
    try:
        with httpx.Client(
            base_url=API_HOST,
            headers={"X-Domino-Api-Key": API_KEY},
            timeout=30,
        ) as c:
            ds_id = _find_dataset_id(c)
            if not ds_id:
                log(f"no dataset found for project {PROJECT_ID}")
                return
            body = {
                "datasetId": ds_id,
                "relativeFilePaths": [DATASET_RELPATH],
            }
            r = c.post("/v4/datasetrw/snapshot", json=body)
            if r.status_code == 200:
                snap = r.json()
                log(
                    f"Domino snapshot created "
                    f"id={snap.get('id')} version={snap.get('version')} "
                    f"status={snap.get('lifecycleStatus')}"
                )
            elif r.status_code == 400 and "already in progress" in r.text:
                log("Domino snapshot already in progress — will catch the next tick")
            else:
                log(f"Domino snapshot API {r.status_code}: {r.text[:300]}")
    except Exception as e:
        log(f"Domino snapshot trigger failed: {e}")


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
                global SNAPSHOT_ROOT, BASEBACKUP_DIR, WAL_DIR, DATASET_RELPATH
                SNAPSHOT_ROOT = Path(_d["snapshot_dir"])
                BASEBACKUP_DIR = SNAPSHOT_ROOT / "basebackup"
                WAL_DIR = SNAPSHOT_ROOT / "wal"
                DATASET_RELPATH = f"db-{DB_ID}"
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

    trigger_domino_snapshot()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
