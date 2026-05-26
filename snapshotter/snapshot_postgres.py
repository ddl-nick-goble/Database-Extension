"""Hourly Postgres snapshotter — runs as a cron inside the DB workspace.

Approach:
  1. Take an online `pg_basebackup` into /domino/datasets/db-<id>/snapshots/<ts>/basebackup/
  2. The WAL archive (continuous, written by Postgres's archive_command in
     postgresql.conf) is already in /domino/datasets/db-<id>/wal/ — we
     prune anything older than the oldest retained snapshot.
  3. Trigger a Domino Dataset snapshot so the version is captured.
  4. Apply tiered retention (rolling hourly/daily/weekly) so we stay under
     the default 20-snapshot dataset cap.

Logs go to /var/log/dd/snapshot.log via cron's stdout/stderr capture.
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
PG_PORT = os.environ.get("DD_PG_PORT", "5432")
PG_USER = os.environ.get("DD_PG_USER", "domino")
PG_PASSWORD = os.environ.get("DD_PG_PASSWORD", "")

# Snapshot path. lifecycle.schedule_snapshotter() pins this via the env-file
# it writes; fall back to the project's default-dataset subdir if unset.
_DEFAULT_ROOT = (
    f"{os.environ.get('DOMINO_DATASETS_DIR', '/mnt/data')}/"
    f"{os.environ.get('DOMINO_PROJECT_NAME', 'default')}/db-{DB_ID}"
)
SNAPSHOT_ROOT = Path(os.environ.get("DD_SNAPSHOT_DIR", _DEFAULT_ROOT))
SNAPSHOTS = SNAPSHOT_ROOT / "snapshots"
WAL = SNAPSHOT_ROOT / "wal"

API_HOST = os.environ.get("DOMINO_API_HOST", "")
API_KEY = os.environ.get("DOMINO_USER_API_KEY", "")
PROJECT_ID = os.environ.get("DOMINO_PROJECT_ID", "")


def log(msg: str) -> None:
    print(f"[snapshot] {dt.datetime.now().isoformat()} {msg}", flush=True)


def basebackup(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PGPASSWORD"] = PG_PASSWORD
    cmd = [
        "/usr/lib/postgresql/16/bin/pg_basebackup",
        "-h", "127.0.0.1",
        "-p", PG_PORT,
        "-U", PG_USER,
        "-D", str(dest),
        "-Ft", "-z",   # tar + gzip → smaller dataset footprint
        "-Xs",          # stream WAL during backup
        "-P",
    ]
    log(f"running pg_basebackup → {dest}")
    subprocess.run(cmd, env=env, check=True)


def trigger_dataset_snapshot(tag: str) -> None:
    """Ask Domino to snapshot the project's default dataset.

    Our snapshot bytes land in a subdir of the project's default dataset
    (DOMINO_DATASETS_DIR/<project>/db-<id>/), so versioning that dataset
    captures them. No-ops if we can't find the dataset — the on-disk bytes
    are still durable, this just adds named version history.
    """
    if not API_HOST or not API_KEY or not PROJECT_ID:
        log("no API credentials/project id — skipping dataset snapshot")
        return
    try:
        with httpx.Client(base_url=API_HOST, headers={"X-Domino-Api-Key": API_KEY}, timeout=15) as c:
            r = c.get("/api/datasetrw/v2/datasets", params={"projectId": PROJECT_ID})
            r.raise_for_status()
            # Response shape: {"datasets": [{"dataset": {...}}, ...], "metadata": ...}
            # The ?projectId filter is ignored on some Domino builds — filter
            # client-side so we don't snapshot somebody else's dataset.
            wrapped = r.json().get("datasets", [])
            mine = [w.get("dataset", {}) for w in wrapped
                    if w.get("dataset", {}).get("projectId") == PROJECT_ID]
            if not mine:
                log(f"no dataset found for project {PROJECT_ID} — skip dataset snapshot")
                return
            ds_id = mine[0].get("id")
            r = c.post(f"/api/datasetrw/v1/datasets/{ds_id}/snapshots", json={"tag": tag})
            if r.status_code >= 400:
                log(f"snapshot API returned {r.status_code}: {r.text}")
            else:
                log(f"dataset snapshot tagged {tag} on dataset {ds_id}")
    except Exception as e:
        log(f"dataset snapshot failed: {e}")


def prune_local() -> None:
    """Tiered retention: keep 6 hourly + 7 daily + 4 weekly = 17."""
    if not SNAPSHOTS.exists():
        return
    now = dt.datetime.now()
    entries = sorted(SNAPSHOTS.iterdir(), key=lambda p: p.name)
    keep: set[Path] = set()

    # Bucket by age category, keep newest in each bucket up to the limit.
    hourly: list[Path] = []
    daily: list[Path] = []
    weekly: list[Path] = []
    for p in entries:
        try:
            ts = dt.datetime.strptime(p.name, "%Y%m%dT%H%M%S")
        except ValueError:
            continue
        age = now - ts
        if age <= dt.timedelta(hours=6):
            hourly.append(p)
        elif age <= dt.timedelta(days=7):
            daily.append(p)
        else:
            weekly.append(p)

    keep.update(hourly[-6:])
    # one per day for daily
    by_day: dict[str, Path] = {}
    for p in daily:
        by_day[p.name[:8]] = p
    keep.update(sorted(by_day.values())[-7:])
    # one per week for weekly
    by_week: dict[str, Path] = {}
    for p in weekly:
        ts = dt.datetime.strptime(p.name, "%Y%m%dT%H%M%S")
        by_week[ts.strftime("%G-W%V")] = p
    keep.update(sorted(by_week.values())[-4:])

    for p in entries:
        if p not in keep and p.is_dir():
            log(f"pruning {p.name}")
            shutil.rmtree(p, ignore_errors=True)


def main() -> int:
    if not PG_PASSWORD:
        log("DD_PG_PASSWORD not set — refusing to run")
        return 2
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    WAL.mkdir(parents=True, exist_ok=True)

    ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    target = SNAPSHOTS / ts / "basebackup"
    try:
        basebackup(target)
    except subprocess.CalledProcessError as e:
        log(f"pg_basebackup failed: {e}")
        shutil.rmtree(SNAPSHOTS / ts, ignore_errors=True)
        return 1

    trigger_dataset_snapshot(tag=ts)
    prune_local()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
