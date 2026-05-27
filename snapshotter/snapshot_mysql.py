"""MySQL snapshotter — mysqldump --single-transaction + gzip + dataset snapshot.

One tick:
  1. mysqldump --single-transaction --routines --triggers | gzip → staging/dump.sql.gz
  2. Atomic rename staging/ → snapshots/<ts>/ and update snapshots/latest symlink
  3. POST /v4/datasetrw/snapshot to capture a versioned Domino snapshot

mysqldump is logical (text SQL). Pros: format-stable, restorable into a
different MySQL build. Cons: slow on big DBs vs binary tools like
XtraBackup. For the playground v1 it's the right tradeoff; v1.1 swaps
to XtraBackup when a real client hits >100 GB.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

DB_ID = os.environ.get("DD_DB_ID") or os.environ.get("DOMINO_RUN_ID", "default")
MYSQL_PORT = os.environ.get("DD_MYSQL_PORT", "3306")
MYSQL_USER = os.environ.get("DD_MYSQL_USER", "domino")
MYSQL_PASSWORD = os.environ.get("DD_MYSQL_PASSWORD", "")

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


def mysqldump(target: Path) -> None:
    """Stream mysqldump | gzip → target. Avoids a temp .sql file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    # --single-transaction: consistent read view (InnoDB), no global lock.
    # --routines + --triggers: captures stored procs / triggers.
    # --no-tablespaces: avoids PROCESS-privilege requirement when our user
    # doesn't have it; users still get a complete restore for the data.
    dump_cmd = [
        "mysqldump",
        "-h", "127.0.0.1", "-P", MYSQL_PORT,
        "-u", MYSQL_USER, f"-p{MYSQL_PASSWORD}",
        "--protocol=TCP",
        "--single-transaction",
        "--routines", "--triggers",
        "--all-databases",
        "--no-tablespaces",
    ]
    log(f"running mysqldump → {target}")
    with target.open("wb") as out:
        dump = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE)
        gz = subprocess.Popen(["gzip", "-1"], stdin=dump.stdout, stdout=out)
        if dump.stdout is not None:
            dump.stdout.close()
        gz_rc = gz.wait()
        dump_rc = dump.wait()
        if dump_rc != 0:
            raise subprocess.CalledProcessError(dump_rc, dump_cmd)
        if gz_rc != 0:
            raise RuntimeError(f"gzip failed rc={gz_rc}")


def swap_into_place(staging: Path, ts: str) -> Path:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    final = SNAPSHOTS_DIR / ts
    if final.exists():
        shutil.rmtree(final, ignore_errors=True)
    staging.rename(final)

    latest = SNAPSHOTS_DIR / "latest"
    tmp_link = SNAPSHOTS_DIR / "latest.new"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(ts)
    tmp_link.rename(latest)
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
    if not MYSQL_PASSWORD:
        log("DD_MYSQL_PASSWORD not set — refusing to run")
        return 2
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    staging = SNAPSHOT_ROOT / f"mysqldump.new.{ts}"
    staging.mkdir(parents=True, exist_ok=True)

    try:
        mysqldump(staging / "dump.sql.gz")
    except subprocess.CalledProcessError as e:
        log(f"mysqldump failed (rc={e.returncode})")
        shutil.rmtree(staging, ignore_errors=True)
        return 1
    except Exception as e:
        log(f"mysqldump pipeline failed: {e}")
        shutil.rmtree(staging, ignore_errors=True)
        return 1

    final = swap_into_place(staging, ts)
    log(f"snapshot ready at {final}")
    trigger_domino_snapshot()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
