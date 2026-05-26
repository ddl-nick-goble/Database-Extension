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
    """Locate this app's config file. Three strategies, in order:
      1. $DD_CONFIG explicit path
      2. /mnt/code/dbapps/$DOMINO_APP_NAME.json
      3. Most recently modified .json under /mnt/code/dbapps/
         (this is the wizard's contract: it writes the config immediately
         before creating the app, so freshest = ours)
    """
    explicit = os.environ.get("DD_CONFIG")
    if explicit and Path(explicit).exists():
        return json.loads(Path(explicit).read_text())

    app_name = os.environ.get("DOMINO_APP_NAME", "")
    if app_name:
        p = DBAPPS_DIR / f"{app_name}.json"
        if p.exists():
            return json.loads(p.read_text())

    if DBAPPS_DIR.exists():
        candidates = sorted(DBAPPS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if candidates:
            sys.stderr.write(f"[lifecycle] picking config {candidates[-1].name} (most recent)\n")
            return json.loads(candidates[-1].read_text())

    raise RuntimeError(
        "No config found. Wizard should have written "
        "/mnt/code/dbapps/<app-name>.json before creating this app."
    )


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
# CloudBeaver — internal :8978, fronted at /admin/
# --------------------------------------------------------------------------
def start_cloudbeaver(cfg: dict) -> None:
    cb_workspace = Path("/mnt/db/.cloudbeaver")
    cb_workspace.mkdir(parents=True, exist_ok=True)
    port = cfg.get("cloudbeaver_port", 8978)

    server_conf = {
        "server": {
            "serverPort": port,
            "serverHost": "127.0.0.1",
            "workspaceLocation": str(cb_workspace),
            "rootURI": "/admin/",
            "serviceURI": "/admin/api/",
        }
    }
    (cb_workspace / "server.conf.json").write_text(json.dumps(server_conf, indent=2))

    if cfg["engine"] == "postgres":
        ds = [{
            "name": "Local Postgres (this database)",
            "driver": "postgres-jdbc",
            "url": f"jdbc:postgresql://localhost:{cfg.get('port', 5432)}/postgres",
            "user": cfg.get("user", "domino"),
            "password": cfg["password"],
            "save-password": True,
        }]
    else:
        ds = [{
            "name": "Local Mongo (this database)",
            "driver": "mongodb",
            "url": f"mongodb://localhost:{cfg.get('port', 27017)}/admin",
            "user": cfg.get("user", "domino"),
            "password": cfg["password"],
            "save-password": True,
        }]
    (cb_workspace / "initial-data-sources.conf").write_text(json.dumps(ds, indent=2))

    log = open("/var/log/dd/cloudbeaver.log", "a")
    subprocess.Popen(
        ["bash", "-c", "cd /opt/cloudbeaver && ./run-server.sh"],
        stdout=log, stderr=log,
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
    start_cloudbeaver(cfg)
    schedule_snapshotter(cfg)
    sys.stderr.write("[lifecycle] all sidecars launched\n")
    return cfg


if __name__ == "__main__":
    cfg = boot()
    print(json.dumps({"booted": True, "engine": cfg["engine"], "db_id": cfg["db_id"]}))
