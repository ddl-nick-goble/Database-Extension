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

DBAPPS_DIR = Path(os.environ.get("DD_DBAPPS_DIR", "/mnt/code/dbapps"))


def find_config() -> dict:
    """Locate this app's config file. Two acceptable sources:
      1. $DD_CONFIG explicit path  (set by the wizard via env vars)
      2. /mnt/code/dbapps/$DOMINO_APP_NAME.json

    If neither resolves, fail loudly. No "most recent file" guessing —
    that risks loading the wrong DB's password when multiple DBs exist
    in one project.
    """
    explicit = os.environ.get("DD_CONFIG")
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise RuntimeError(f"DD_CONFIG={explicit} but file does not exist")
        return json.loads(p.read_text())

    app_name = os.environ.get("DOMINO_APP_NAME", "")
    if not app_name:
        raise RuntimeError(
            "Neither DD_CONFIG nor DOMINO_APP_NAME is set. "
            "Wizard must inject one of them when creating the App."
        )
    p = DBAPPS_DIR / f"{app_name}.json"
    if not p.exists():
        raise RuntimeError(
            f"Expected config at {p} (DOMINO_APP_NAME={app_name}) but file is missing. "
            f"Did the wizard fail to write it before creating the App?"
        )
    return json.loads(p.read_text())


# --------------------------------------------------------------------------
# Postgres
# --------------------------------------------------------------------------
PGCTL = "/usr/lib/postgresql/16/bin/pg_ctl"
INITDB = "/usr/lib/postgresql/16/bin/initdb"


def restore_or_init_postgres(cfg: dict) -> None:
    pgdata = Path(cfg.get("pgdata", "/mnt/db/pgdata"))
    snapshot_dir = Path(f"/domino/datasets/db-{cfg['db_id']}")
    pgdata.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "wal").mkdir(exist_ok=True)

    if any(pgdata.iterdir()):
        sys.stderr.write(f"[lifecycle] pgdata already populated, skipping restore/init\n")
        return

    snapshots = snapshot_dir / "snapshots"
    if snapshots.exists():
        latest = sorted(snapshots.iterdir(), key=lambda p: p.name)
        latest = next((p for p in reversed(latest) if (p / "basebackup").exists()), None)
        if latest:
            sys.stderr.write(f"[lifecycle] restoring pgdata from snapshot {latest.name}\n")
            shutil.copytree(latest / "basebackup", pgdata, dirs_exist_ok=True)
            (pgdata / "recovery.signal").touch()
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
    with (pgdata / "postgresql.conf").open("a") as f:
        f.write(f"\nlisten_addresses = '127.0.0.1'\nport = {port}\n")
        f.write("archive_mode = on\n")
        f.write(f"archive_command = 'test ! -f {snapshot_dir}/wal/%f && cp %p {snapshot_dir}/wal/%f'\n")
        f.write("wal_level = replica\nmax_wal_senders = 3\n")
    with (pgdata / "pg_hba.conf").open("a") as f:
        f.write("host all all 127.0.0.1/32 scram-sha-256\n")


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
    interval = cfg.get("snapshot_interval_min", 60)
    script = f"/mnt/code/snapshotter/snapshot_{cfg['engine']}.py"
    line = f"*/{interval} * * * * /usr/bin/python3 {script} >> /var/log/dd/snapshot.log 2>&1\n"
    # Reset crontab to only our entry
    subprocess.run(["bash", "-c", f"echo '{line}' | crontab -"], check=False)


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
