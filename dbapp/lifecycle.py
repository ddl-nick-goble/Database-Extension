"""DB app lifecycle — restore, start, supervise.

Each Domino DB App's container goes through these steps on boot:

  1. Load per-DB config (engine, password, db_id) from /mnt/code/dbapps/<name>.json
     — written by the wizard before it created this app.
  2. Restore /mnt/db/<engine>data from the latest dataset snapshot, OR init fresh.
  3. Start Postgres (or MongoDB) listening on 127.0.0.1.
  4. Start CloudBeaver on 127.0.0.1:8978 with the local DB pre-configured.
  5. Start the snapshotter cron.

The Flask router (dbapp/router.py) then takes over port 8888 and fronts:
  /        → status page
  /wire    → ws2tcp WebSocket relay → localhost:<engine port>
  /admin/* → reverse-proxy → localhost:8978 (CloudBeaver)
  /api/*   → status JSON
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

def _default_dbapps_dir() -> Path:
    """Where per-DB config files live. Default is a subdir of the project's
    default dataset so any project using dd-postgres-app can read configs
    without depending on /mnt/code being this repo."""
    base = os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data")
    project = os.environ.get("DOMINO_PROJECT_NAME", "default")
    return Path(base) / project / "_dd_configs"


DBAPPS_DIR = Path(os.environ.get("DD_DBAPPS_DIR")) if os.environ.get("DD_DBAPPS_DIR") else _default_dbapps_dir()


def find_config() -> dict:
    """Locate this App's config file. Resolution order:

      1. $DD_CONFIG explicit path  (set by the wizard via env vars)
      2. /mnt/code/dbapps/$DOMINO_APP_NAME.json
      3. The most recently-modified .json in /mnt/code/dbapps/

    The fallback (3) exists because Domino's App containers do NOT receive
    DOMINO_APP_NAME — confirmed by inspecting /var/lib/domino/launch/env.sh
    inside a running App. The wizard writes the config right before POSTing
    /start, so "most recent" is reliably the just-created DB's config.

    Caveat: if you provision two DBs in the same project at the same time,
    both Apps will see the same newest config file and one will silently
    pick up the other's credentials. The wizard guards against this with a
    name-collision check; the race window is only the few seconds between
    POST /api/apps/beta/apps and POST /v4/modelProducts/<id>/start.
    """
    explicit = os.environ.get("DD_CONFIG")
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise RuntimeError(f"DD_CONFIG={explicit} but file does not exist")
        sys.stderr.write(f"[lifecycle] config from DD_CONFIG={p}\n")
        return json.loads(p.read_text())

    app_name = os.environ.get("DOMINO_APP_NAME", "")
    if app_name:
        p = DBAPPS_DIR / f"{app_name}.json"
        if p.exists():
            sys.stderr.write(f"[lifecycle] config from DOMINO_APP_NAME={app_name}\n")
            return json.loads(p.read_text())
        sys.stderr.write(
            f"[lifecycle] DOMINO_APP_NAME={app_name} but {p} missing — falling back to newest .json\n"
        )

    # Look in the primary dir, then the legacy /mnt/code/dbapps/ for dev iteration.
    search_dirs = [DBAPPS_DIR, Path("/mnt/code/dbapps")]
    candidates: list[Path] = []
    for d in search_dirs:
        if d.exists():
            candidates.extend(d.glob("*.json"))
    candidates = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError(
            f"No config file found in any of {[str(d) for d in search_dirs]}. "
            f"Did the wizard fail to write one?"
        )
    sys.stderr.write(f"[lifecycle] config from most-recent fallback: {candidates[0]}\n")
    return json.loads(candidates[0].read_text())


# --------------------------------------------------------------------------
# Postgres
# --------------------------------------------------------------------------
PGCTL = "/usr/lib/postgresql/16/bin/pg_ctl"
INITDB = "/usr/lib/postgresql/16/bin/initdb"


def snapshot_path(cfg: dict) -> Path:
    """Where this DB's snapshots live. Always a subdir of the project's default
    dataset on this Domino instance (DOMINO_DATASETS_DIR=/mnt/data), keyed by
    db_id so multiple DBs in one project don't collide.

    Single source of truth — lifecycle.py uses it for restore, snapshotter
    reads $DD_SNAPSHOT_DIR (which we set from this).
    """
    explicit = cfg.get("snapshot_dir") or os.environ.get("DD_SNAPSHOT_DIR")
    if explicit:
        return Path(explicit)
    base = os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data")
    project = os.environ.get("DOMINO_PROJECT_NAME", "default")
    return Path(base) / project / f"db-{cfg['db_id']}"


def restore_or_init_postgres(cfg: dict) -> None:
    pgdata = Path(cfg.get("pgdata", "/mnt/db/pgdata"))
    snapshot_dir = snapshot_path(cfg)
    pgdata.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "wal").mkdir(exist_ok=True)

    if any(pgdata.iterdir()):
        sys.stderr.write(f"[lifecycle] pgdata already populated, skipping restore/init\n")
        return

    # Restore from the single canonical basebackup path. Domino dataset
    # snapshots are the version history; the live path is always the most
    # recent successful basebackup.
    basebackup = snapshot_dir / "basebackup"
    base_tar = basebackup / "base.tar.gz"
    wal_tar = basebackup / "pg_wal.tar.gz"
    if base_tar.exists() and wal_tar.exists():
        sys.stderr.write(f"[lifecycle] restoring pgdata from {basebackup}\n")
        subprocess.run(["tar", "-xzf", str(base_tar), "-C", str(pgdata)], check=True)
        (pgdata / "pg_wal").mkdir(exist_ok=True)
        subprocess.run(["tar", "-xzf", str(wal_tar), "-C", str(pgdata / "pg_wal")], check=True)
        _pin_socket_dir(pgdata, cfg)
        return

    # Legacy timestamped layout (db-<id>/snapshots/<ts>/basebackup/) — kept
    # only so DBs from before this refactor can still cold-boot. Will be
    # gone once no live DB references it.
    legacy = snapshot_dir / "snapshots"
    if legacy.exists():
        candidates = sorted(legacy.iterdir(), key=lambda p: p.name)
        latest = next(
            (p for p in reversed(candidates)
             if (p / "basebackup" / "base.tar.gz").exists()),
            None,
        )
        if latest:
            sys.stderr.write(f"[lifecycle] restoring pgdata from LEGACY snapshot {latest.name}\n")
            subprocess.run(["tar", "-xzf", str(latest / "basebackup" / "base.tar.gz"), "-C", str(pgdata)], check=True)
            (pgdata / "pg_wal").mkdir(exist_ok=True)
            subprocess.run(["tar", "-xzf", str(latest / "basebackup" / "pg_wal.tar.gz"), "-C", str(pgdata / "pg_wal")], check=True)
            _pin_socket_dir(pgdata, cfg)
            return

    sys.stderr.write("[lifecycle] no snapshot, initializing fresh cluster\n")
    pwfile = pgdata.parent / ".pwfile"
    pwfile.write_text(cfg["password"])
    pwfile.chmod(0o600)
    subprocess.run([
        INITDB, "-D", str(pgdata),
        "--auth-host=scram-sha-256", "--auth-local=trust",
        f"--username={cfg.get('user', 'domino')}",
        f"--pwfile={pwfile}",
    ], check=True)
    pwfile.unlink()

    port = cfg.get("port", 5432)
    socket_dir = cfg.get("socket_dir", "/mnt/db/sock")
    Path(socket_dir).mkdir(parents=True, exist_ok=True)
    with (pgdata / "postgresql.conf").open("a") as f:
        f.write(f"\nlisten_addresses = '127.0.0.1'\nport = {port}\n")
        # Default /var/run/postgresql is owned by the postgres OS user; we run
        # as ubuntu and can't write a lock file there. Pin to a path we own.
        f.write(f"unix_socket_directories = '{socket_dir}'\n")
        f.write("archive_mode = on\n")
        f.write(f"archive_command = 'test ! -f {snapshot_dir}/wal/%f && cp %p {snapshot_dir}/wal/%f'\n")
        f.write("wal_level = replica\nmax_wal_senders = 3\n")
    with (pgdata / "pg_hba.conf").open("a") as f:
        f.write("host all all 127.0.0.1/32 scram-sha-256\n")


def _pin_socket_dir(pgdata: Path, cfg: dict) -> None:
    """Restored snapshots carry the socket-dir from the snapshot-source cluster.
    Re-pin to a path the current process can actually write."""
    socket_dir = cfg.get("socket_dir", "/mnt/db/sock")
    Path(socket_dir).mkdir(parents=True, exist_ok=True)
    conf = pgdata / "postgresql.conf"
    existing = conf.read_text() if conf.exists() else ""
    if f"unix_socket_directories = '{socket_dir}'" not in existing:
        with conf.open("a") as f:
            f.write(f"\nunix_socket_directories = '{socket_dir}'\n")


def start_postgres(cfg: dict) -> None:
    pgdata = cfg.get("pgdata", "/mnt/db/pgdata")
    port = cfg.get("port", 5432)
    subprocess.run([
        PGCTL, "-D", pgdata, "-l", "/var/log/dd/postgres.log",
        "-o", f"-p {port}", "start",
    ], check=True)
    # Wait for readiness
    for _ in range(30):
        r = subprocess.run(["pg_isready", "-h", "127.0.0.1", "-p", str(port), "-q"])
        if r.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("Postgres failed to become ready in 30s")


# --------------------------------------------------------------------------
# Mongo
# --------------------------------------------------------------------------
def restore_or_init_mongo(cfg: dict) -> str:
    """Returns 'fresh' | 'restore' | 'noop' to inform post-start steps."""
    mongo_data = Path(cfg.get("data", "/mnt/db/mongo"))
    snapshot_dir = Path(f"/domino/datasets/db-{cfg['db_id']}")
    mongo_data.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    if any(mongo_data.iterdir()):
        return "noop"
    snapshots = snapshot_dir / "snapshots"
    if snapshots.exists() and any(snapshots.iterdir()):
        return "restore"
    return "fresh"


def start_mongo(cfg: dict) -> str:
    mongo_data = cfg.get("data", "/mnt/db/mongo")
    port = cfg.get("port", 27017)
    state = restore_or_init_mongo(cfg)
    subprocess.run([
        "mongod", "--dbpath", mongo_data,
        "--bind_ip", "127.0.0.1", "--port", str(port),
        "--logpath", "/var/log/dd/mongod.log", "--fork",
    ], check=True)
    for _ in range(30):
        r = subprocess.run(
            ["mongosh", "--quiet", "--port", str(port), "--eval", "db.runCommand({ping:1}).ok"],
            capture_output=True, text=True,
        )
        if "1" in r.stdout:
            break
        time.sleep(1)
    else:
        raise RuntimeError("mongod failed to become ready in 30s")

    if state == "fresh":
        sys.stderr.write("[lifecycle] creating admin user\n")
        user = cfg.get("user", "domino")
        script = f"""
db.createUser({{
  user: "{user}",
  pwd: "{cfg['password']}",
  roles: [{{role: "root", db: "admin"}}]
}})
"""
        subprocess.run(
            ["mongosh", "--quiet", "--port", str(port), "admin"],
            input=script, text=True, check=True,
        )
    elif state == "restore":
        snapshot_dir = Path(f"/domino/datasets/db-{cfg['db_id']}/snapshots")
        latest = sorted(snapshot_dir.iterdir(), key=lambda p: p.name)[-1]
        sys.stderr.write(f"[lifecycle] mongorestore from {latest.name}\n")
        subprocess.run([
            "mongorestore", "--port", str(port),
            "--gzip", "--oplogReplay", str(latest),
        ], check=True)
    return state


# --------------------------------------------------------------------------
# pgweb — Go-binary OSS Postgres admin, internal :8978, fronted at /admin/
# --------------------------------------------------------------------------
def start_pgweb(cfg: dict) -> None:
    """Start pgweb pre-connected to the local Postgres.

    pgweb (/usr/local/bin/pgweb, pinned to v0.17.0 in the env image) is a
    single Go binary that serves a schema/SQL/edit UI on its own port. The
    --prefix flag makes it generate /admin-prefixed asset URLs so our
    Flask reverse-proxy at /admin/ Just Works.
    """
    if cfg["engine"] != "postgres":
        return  # pgweb is Postgres-only; Mongo admin lands in v1.
    if not Path("/usr/local/bin/pgweb").exists():
        raise RuntimeError("pgweb missing at /usr/local/bin/pgweb — rebuild dd-postgres-app")
    port = cfg.get("admin_port", 8978)
    pg_port = cfg.get("port", 5432)
    user = cfg.get("user", "domino")
    pw = cfg["password"]
    url = f"postgres://{user}:{pw}@127.0.0.1:{pg_port}/postgres?sslmode=disable"
    log_path = open("/var/log/dd/pgweb.log", "a")
    subprocess.Popen(
        ["pgweb",
         "--bind", "127.0.0.1",
         "--listen", str(port),
         "--prefix", "admin",          # serve at /admin/...
         "--url", url,
         "--skip-open",
         "--lock-session"],            # one DB per pgweb instance — ours
        stdout=log_path, stderr=log_path,
    )


# --------------------------------------------------------------------------
# Cron snapshotter
# --------------------------------------------------------------------------
def schedule_snapshotter(cfg: dict) -> None:
    """Spawn a detached background subprocess that runs the snapshotter on a
    fixed interval. We used to install a crontab entry, but cron-as-ubuntu
    doesn't fire without sudoers configuration, so the entry was effectively
    a no-op. A self-contained shell loop in a new session is root-free and
    survives the parent's exec to gunicorn.
    """
    interval_min = int(cfg.get("snapshot_interval_min", 60))
    # Prefer /mnt/code/snapshotter/ (live from this commit) over the baked
    # /opt/dd/snapshotter/ — same dev-iteration preference as the dispatcher
    # picks /mnt/code/dbapp/ over /opt/dd/.
    script = None
    for candidate in (
        f"/mnt/code/snapshotter/snapshot_{cfg['engine']}.py",
        f"/opt/dd/snapshotter/snapshot_{cfg['engine']}.py",
    ):
        if Path(candidate).exists():
            script = candidate
            break
    if not script:
        sys.stderr.write(f"[lifecycle] WARN: snapshotter not found for engine={cfg['engine']}\n")
        return

    snap_dir = snapshot_path(cfg)
    env = os.environ.copy()
    env.update({
        "DD_DB_ID": cfg["db_id"],
        "DD_SNAPSHOT_DIR": str(snap_dir),
        "DD_PG_PORT": str(cfg.get("port", 5432)),
        "DD_PG_USER": cfg.get("user", "domino"),
        "DD_PG_PASSWORD": cfg["password"],
    })

    interval_sec = max(60, interval_min * 60)
    log_path = "/var/log/dd/snapshot.log"
    # In-dataset log mirror — survives container exit, queryable from
    # other workspaces in the same project for debugging.
    diag_dir = snap_dir / "_diag"
    diag_dir.mkdir(parents=True, exist_ok=True)
    out_log = diag_dir / "snapshot.out"
    loop_cmd = (
        f"sleep 5; "
        f"while true; do "
        f"  echo \"--- $(date -u +%Y-%m-%dT%H:%M:%SZ) ---\" >> {out_log}; "
        f"  python3 {script} >> {out_log} 2>&1; "
        f"  echo \"--- rc=$? ---\" >> {out_log}; "
        f"  sleep {interval_sec}; "
        f"done"
    )
    with open(log_path, "a") as logf:
        proc = subprocess.Popen(
            ["bash", "-c", loop_cmd],
            env=env,
            stdout=logf, stderr=logf,
            start_new_session=True,   # survive parent exit
            close_fds=True,
        )
    sys.stderr.write(f"[lifecycle] snapshotter loop started pid={proc.pid} (every {interval_sec}s, script={script})\n")

    # Also stage a ready-to-run script for the SIGTERM trap in dbapp/app.sh
    # to call on graceful shutdown — captures the last write before the
    # container disappears. Has the same env vars baked in so it can run
    # without any state from the python parent.
    final_helper = Path("/tmp/dd-final-snapshot.sh")
    final_helper.write_text(
        "#!/bin/bash\n"
        "# Run by dbapp/app.sh on SIGTERM/SIGINT. Bake a fresh snapshot\n"
        "# before the container is killed.\n"
        f"export DD_DB_ID={cfg['db_id']!r}\n"
        f"export DD_SNAPSHOT_DIR={str(snap_dir)!r}\n"
        f"export DD_PG_PORT={cfg.get('port', 5432)}\n"
        f"export DD_PG_USER={cfg.get('user', 'domino')!r}\n"
        f"export DD_PG_PASSWORD={cfg['password']!r}\n"
        f"export DOMINO_API_PROXY={os.environ.get('DOMINO_API_PROXY', 'http://localhost:8899')!r}\n"
        f"export DOMINO_USER_API_KEY={os.environ.get('DOMINO_USER_API_KEY', '')!r}\n"
        f"export DOMINO_PROJECT_ID={os.environ.get('DOMINO_PROJECT_ID', '')!r}\n"
        f"timeout 60 python3 {script} 2>&1\n"
    )
    final_helper.chmod(0o700)
    sys.stderr.write(f"[lifecycle] teardown snapshot helper written to {final_helper}\n")


# --------------------------------------------------------------------------
# Entry — invoked by dbapp/app.sh once before launching the Flask router
# --------------------------------------------------------------------------
def boot() -> dict:
    Path("/var/log/dd").mkdir(parents=True, exist_ok=True)
    cfg = find_config()
    sys.stderr.write(f"[lifecycle] booting engine={cfg['engine']} db_id={cfg['db_id']}\n")
    if cfg["engine"] == "postgres":
        restore_or_init_postgres(cfg)
        start_postgres(cfg)
    elif cfg["engine"] == "mongo":
        start_mongo(cfg)
    else:
        raise RuntimeError(f"unsupported engine: {cfg['engine']}")
    start_pgweb(cfg)
    schedule_snapshotter(cfg)
    sys.stderr.write("[lifecycle] all sidecars launched\n")
    return cfg


if __name__ == "__main__":
    cfg = boot()
    print(json.dumps({"booted": True, "engine": cfg["engine"], "db_id": cfg["db_id"]}))
