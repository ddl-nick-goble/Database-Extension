"""Redis snapshotter — BGSAVE + gzip dump.rdb + Domino dataset snapshot.

One tick:
  1. Record current LASTSAVE timestamp on the server
  2. Send BGSAVE; poll LASTSAVE until it advances (snapshot complete)
  3. Read the live dump.rdb path from CONFIG GET dir + dbfilename
  4. gzip the .rdb into staging, atomic-rename into snapshots/<ts>/
  5. Update snapshots/latest symlink
  6. POST /v4/datasetrw/snapshot for the versioned Domino snapshot

The live AOF (appendfsync everysec) is what protects against in-memory
loss between snapshots — the dataset snapshot exists for point-in-time
restore on cold boot in a new DB App.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

DB_ID = os.environ.get("DD_DB_ID") or os.environ.get("DOMINO_RUN_ID", "default")
REDIS_PORT = os.environ.get("DD_REDIS_PORT", "6379")
REDIS_PASSWORD = os.environ.get("DD_REDIS_PASSWORD", "")

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


def redis_cli(*args: str, timeout: int = 10) -> str:
    cmd = [
        "redis-cli", "-h", "127.0.0.1", "-p", REDIS_PORT,
        "-a", REDIS_PASSWORD, "--no-auth-warning",
    ] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(
            f"redis-cli {args[0]} failed (rc={r.returncode}): {r.stderr[:300]}"
        )
    return r.stdout.strip()


def _rdb_path() -> Path:
    """Read dir + dbfilename from the running server. Avoids hardcoding
    /mnt/db/redis — the adapter could move it."""
    out = redis_cli("CONFIG", "GET", "dir").splitlines()
    # CONFIG GET returns key\nvalue\n
    rdb_dir = out[-1] if out else "/mnt/db/redis"
    out = redis_cli("CONFIG", "GET", "dbfilename").splitlines()
    rdb_name = out[-1] if out else "dump.rdb"
    return Path(rdb_dir) / rdb_name


def bgsave_and_wait(staging: Path) -> Path:
    """Trigger BGSAVE, wait for LASTSAVE to advance, copy + gzip the .rdb."""
    before = int(redis_cli("LASTSAVE"))
    redis_cli("BGSAVE")
    log("BGSAVE triggered; waiting for LASTSAVE to advance")
    # Default: poll for up to 5 minutes. Larger RDBs may need this.
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(2)
        try:
            now = int(redis_cli("LASTSAVE"))
        except RuntimeError:
            continue
        if now > before:
            break
    else:
        raise RuntimeError("BGSAVE never completed within 5 minutes")

    rdb = _rdb_path()
    if not rdb.exists():
        raise RuntimeError(f"rdb file not found at {rdb}")

    staging.mkdir(parents=True, exist_ok=True)
    target = staging / "dump.rdb.gz"
    log(f"compressing {rdb} → {target}")
    with rdb.open("rb") as src, subprocess.Popen(
        ["gzip", "-1"], stdin=subprocess.PIPE, stdout=target.open("wb"),
    ) as gz:
        shutil.copyfileobj(src, gz.stdin)
        if gz.stdin is not None:
            gz.stdin.close()
        gz_rc = gz.wait()
    if gz_rc != 0:
        raise RuntimeError(f"gzip failed rc={gz_rc}")
    return target


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
    if not REDIS_PASSWORD:
        log("DD_REDIS_PASSWORD not set — refusing to run")
        return 2
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    staging = SNAPSHOT_ROOT / f"bgsave.new.{ts}"

    try:
        bgsave_and_wait(staging)
    except RuntimeError as e:
        log(f"BGSAVE pipeline failed: {e}")
        shutil.rmtree(staging, ignore_errors=True)
        return 1

    final = swap_into_place(staging, ts)
    log(f"snapshot ready at {final}")
    trigger_domino_snapshot()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
