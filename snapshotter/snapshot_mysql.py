"""MySQL snapshotter — mysqldump --single-transaction + gzip + dataset snapshot.

One tick:
  1. mysqldump --single-transaction --routines --triggers | gzip → staging/dump.sql.gz
  2. Atomic rename staging/ → snapshots/<ts>/ and update snapshots/latest symlink
  3. Capture a versioned Domino dataset snapshot (see
     _dataset_snapshot.trigger_domino_snapshot — engine-agnostic).

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

from _dataset_snapshot import trigger_domino_snapshot

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


def main() -> int:
    # Pick up runtime backup path change without requiring container restart.
    _override = Path("/tmp/dd-backup-override.json")
    if _override.exists():
        try:
            import json as _json
            _d = _json.loads(_override.read_text())
            if _d.get("db_id") == DB_ID and _d.get("snapshot_dir"):
                global SNAPSHOT_ROOT, SNAPSHOTS_DIR
                SNAPSHOT_ROOT = Path(_d["snapshot_dir"])
                SNAPSHOTS_DIR = SNAPSHOT_ROOT / "snapshots"
        except Exception:
            pass

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
    trigger_domino_snapshot(
        api_host=API_HOST, api_key=API_KEY, project_id=PROJECT_ID,
        snapshot_root=SNAPSHOT_ROOT, datasets_dir=_DOMINO_DATASETS_DIR, log=log,
    )
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
