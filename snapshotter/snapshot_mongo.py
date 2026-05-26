"""Hourly MongoDB snapshotter — runs as cron inside the DB workspace.

mongodump --oplog gives a consistent snapshot even with concurrent writes;
the oplog tail captures anything that landed during the dump.
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
MONGO_PORT = os.environ.get("DD_MONGO_PORT", "27017")
MONGO_USER = os.environ.get("DD_MONGO_USER", "domino")
MONGO_PASSWORD = os.environ.get("DD_MONGO_PASSWORD", "")

SNAPSHOT_ROOT = Path(f"/domino/datasets/db-{DB_ID}")
SNAPSHOTS = SNAPSHOT_ROOT / "snapshots"

API_HOST = os.environ.get("DOMINO_API_HOST", "")
API_KEY = os.environ.get("DOMINO_USER_API_KEY", "")
PROJECT_ID = os.environ.get("DOMINO_PROJECT_ID", "")


def log(msg: str) -> None:
    print(f"[snapshot] {dt.datetime.now().isoformat()} {msg}", flush=True)


def mongodump(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [
        "mongodump",
        f"--host=127.0.0.1:{MONGO_PORT}",
        f"--username={MONGO_USER}",
        f"--password={MONGO_PASSWORD}",
        "--authenticationDatabase=admin",
        "--oplog",
        "--gzip",
        f"--out={dest}",
    ]
    log(f"running mongodump → {dest}")
    subprocess.run(cmd, check=True)


def trigger_dataset_snapshot(tag: str) -> None:
    if not API_HOST or not API_KEY:
        return
    try:
        with httpx.Client(base_url=API_HOST, headers={"X-Domino-Api-Key": API_KEY}, timeout=15) as c:
            r = c.get("/api/datasetrw/v2/datasets", params={"projectId": PROJECT_ID})
            r.raise_for_status()
            datasets = r.json().get("data", r.json())
            target = next((d for d in datasets if d.get("name") == f"db-{DB_ID}"), None)
            if not target:
                return
            ds_id = target.get("id") or target.get("datasetId")
            c.post(f"/api/datasetrw/v1/datasets/{ds_id}/snapshots", json={"tag": tag})
    except Exception as e:
        log(f"dataset snapshot failed: {e}")


def main() -> int:
    if not MONGO_PASSWORD:
        log("DD_MONGO_PASSWORD not set — refusing to run")
        return 2
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    target = SNAPSHOTS / ts
    try:
        mongodump(target)
    except subprocess.CalledProcessError as e:
        log(f"mongodump failed: {e}")
        shutil.rmtree(target, ignore_errors=True)
        return 1
    trigger_dataset_snapshot(tag=ts)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
