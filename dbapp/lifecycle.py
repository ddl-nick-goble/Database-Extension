"""DB app lifecycle — restore, start, supervise.

Each Domino DB App's container goes through these steps on boot:

  1. Load per-DB config (engine, password, db_id) from a per-DB JSON
     written by the wizard before it created this app.
  2. Hand off to the EngineAdapter for the engine cfg names — it owns
     restore-or-init, start, shutdown, health and admin-UI specs.
  3. Launch the adapter-described admin UI (pgweb / mongo-express /
     phpMyAdmin / redis-commander) on the admin port.
  4. Schedule the per-engine snapshotter.

The Flask router (dbapp/router.py) then takes over port 8888 and fronts:
  /        → status page
  /wire    → ws2tcp WebSocket relay → localhost:<engine port>
  /admin/* → reverse-proxy → localhost:<admin port>
  /api/*   → status JSON
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

def find_config() -> dict:
    """Locate this App's config.

    The wizard stashes the config as a base64-JSON project env var keyed by the
    App's STABLE id: DD_CFG_<app_id>, set at create time (before the container
    exists). At boot we resolve our own app_id from DOMINO_RUN_ID via the Domino
    API and read that var.

    Why not key by run_id: the run/instance id is only assigned when the
    container launches, changes on every (re)start, and the wizard can't know it
    ahead of time — so it can't be the key. The app_id is stable.

    DD_CONFIG_JSON (inline) is honored first for manual / self-contained runs.
    """
    sys.stderr.write("[lifecycle] find_config build=appid-v3\n")

    inline = os.environ.get("DD_CONFIG_JSON", "").strip()
    if inline:
        sys.stderr.write("[lifecycle] config from DD_CONFIG_JSON env var\n")
        return json.loads(inline)

    run_id = os.environ.get("DOMINO_RUN_ID", "")
    app_id = _resolve_app_id(run_id) if run_id else ""
    if app_id:
        cfg = _decode_cfg_env(f"DD_CFG_{app_id.upper()}")
        if cfg is not None:
            sys.stderr.write(f"[lifecycle] config from project env DD_CFG_{app_id}\n")
            return cfg

    sys.stderr.write(_config_diagnostics(run_id, app_id) + "\n")
    raise RuntimeError(
        f"No config: could not resolve a config for run_id={run_id or '<unset>'} "
        f"(resolved app_id={app_id or '<none>'}). See diagnostics above."
    )


def _decode_cfg_env(var_name: str) -> dict | None:
    """Decode a base64-JSON config from project env var `var_name`, or None if
    it is unset/invalid."""
    raw = os.environ.get(var_name, "")
    if not raw:
        return None
    import base64
    try:
        return json.loads(base64.b64decode(raw).decode())
    except Exception as e:
        sys.stderr.write(f"[lifecycle] {var_name} present but undecodable: {e}\n")
        return None


def _domino_api_bases() -> list[str]:
    """Domino API base URLs to try, in order: the in-pod proxy first, then the
    public nucleus host (reachable over the network, independent of the local
    sidecar's startup timing)."""
    bases: list[str] = []
    proxy = os.environ.get("DOMINO_API_PROXY", "http://localhost:8899")
    if proxy:
        bases.append(proxy.rstrip("/"))
    host = (os.environ.get("DOMINO_API_HOST")
            or os.environ.get("DOMINO_PUBLIC_HOST", "")).rstrip("/")
    if host and host not in bases:
        bases.append(host)
    return bases


def _fetch_apps_via_api() -> list[dict]:
    """List this project's Apps via the Domino API (self-contained: `requests`
    + standard DOMINO_* env vars, no domino_api import)."""
    api_key = os.environ.get("DOMINO_USER_API_KEY", "")
    project_id = os.environ.get("DOMINO_PROJECT_ID", "")
    if not (api_key and project_id):
        return []
    import requests
    for base in _domino_api_bases():
        try:
            r = requests.get(
                f"{base}/v4/modelProducts",
                params={"projectId": project_id},
                headers={"X-Domino-Api-Key": api_key},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            sys.stderr.write(f"[lifecycle] app list via API failed ({base}): {e}\n")
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "items", "results"):
                if isinstance(data.get(k), list):
                    return data[k]
        return []
    return []


def _app_id_from_apps(apps: list[dict], run_id: str) -> str:
    """Find the app whose running instance matches `run_id`. In the
    /v4/modelProducts schema the instance id is `latestAppInstanceId`."""
    for a in apps:
        if a.get("latestAppInstanceId") == run_id:
            return a.get("id", "")
        for key in ("runningInstanceId", "currentInstanceId", "appInstanceId"):
            if a.get(key) == run_id:
                return a.get("id", "")
    return ""


def _resolve_app_id(run_id: str, timeout_s: float = 60, poll_s: float = 3) -> str:
    """Map DOMINO_RUN_ID -> app_id via the Domino API. The in-pod proxy may not
    be listening yet at boot, so retry until it (or the public host) answers and
    this app appears in the listing."""
    import time as _time
    deadline = _time.monotonic() + timeout_s
    while True:
        app_id = _app_id_from_apps(_fetch_apps_via_api(), run_id)
        if app_id:
            return app_id
        if _time.monotonic() >= deadline:
            return ""
        sys.stderr.write("[lifecycle] API not ready / app not listed yet — waiting…\n")
        _time.sleep(poll_s)


def _config_diagnostics(run_id: str, app_id: str) -> str:
    """Dump everything find_config used, for the boot log."""
    L = ["[lifecycle] ---- config diagnostics ----"]
    L.append(
        f"  ids: DOMINO_RUN_ID={run_id or '<unset>'} resolved_app_id={app_id or '<none>'} "
        f"DOMINO_PROJECT_ID={'set' if os.environ.get('DOMINO_PROJECT_ID') else '<unset>'} "
        f"DOMINO_USER_API_KEY={'set' if os.environ.get('DOMINO_USER_API_KEY') else '<unset>'}"
    )
    cfg_vars = sorted(k for k in os.environ if k.startswith("DD_CFG_"))
    L.append(f"  DD_CFG_* present={cfg_vars or '<none>'}")
    if app_id:
        want = f"DD_CFG_{app_id.upper()}"
        L.append(f"  expected var {want} "
                 f"{'PRESENT' if os.environ.get(want) else 'MISSING — wizard did not set it'}")
    try:
        apps = _fetch_apps_via_api()
        L.append(f"  API returned {len(apps)} app(s); looking for run_id={run_id}:")
        for a in apps[:12]:
            L.append(f"      app id={a.get('id')} latestAppInstanceId={a.get('latestAppInstanceId')} "
                     f"status={a.get('status')}")
    except Exception as e:
        L.append(f"  API probe errored: {e}")
    L.append("[lifecycle] ---- end diagnostics ----")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Postgres
# --------------------------------------------------------------------------
PGCTL = "/usr/lib/postgresql/16/bin/pg_ctl"
INITDB = "/usr/lib/postgresql/16/bin/initdb"


def snapshot_path(cfg: dict) -> Path:
    """Where this DB's snapshots live.

    Reads DD_SNAPSHOT_<DB_ID_UPPER> from the project's environment variables
    (set by the wizard at creation time via the Domino project env vars API).
    Falls back to the project dataset path derived from DOMINO_PROJECT_NAME.
    """
    db_id = cfg["db_id"]
    # cfg["snapshot_dir"] is set by load_backup_override (user edits via UI persist here).
    if cfg.get("snapshot_dir"):
        return Path(cfg["snapshot_dir"])
    snap_var = f"DD_SNAPSHOT_{db_id.replace('-', '_').upper()}"
    explicit = os.environ.get(snap_var) or os.environ.get("DD_SNAPSHOT_DIR")
    if explicit:
        return Path(explicit)
    base = os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data")
    project = os.environ.get("DOMINO_PROJECT_NAME", "default")
    return Path(base) / project / f"db-{db_id}"


_BACKUP_OVERRIDE = Path("/tmp/dd-backup-override.json")


def load_backup_override(cfg: dict) -> dict:
    """Merge a persisted backup config (snapshot_dir) into cfg.

    Checks:
      1. /tmp/dd-backup-override.json (set by the router at runtime)
      2. {DATASETS_DIR}/*/_dd_backup_config.json (survives container restart)

    Returns updated cfg dict (caller should replace their reference).
    """
    db_id = cfg.get("db_id", "")

    # Fast path: in-memory override written by the router this session.
    if _BACKUP_OVERRIDE.exists():
        try:
            data = json.loads(_BACKUP_OVERRIDE.read_text())
            if data.get("db_id") == db_id and data.get("snapshot_dir"):
                cfg = dict(cfg)
                cfg["snapshot_dir"] = data["snapshot_dir"]
                sys.stderr.write(
                    f"[lifecycle] backup override from {_BACKUP_OVERRIDE}: "
                    f"snapshot_dir={cfg['snapshot_dir']}\n"
                )
                return cfg
        except Exception as e:
            sys.stderr.write(f"[lifecycle] ignoring malformed {_BACKUP_OVERRIDE}: {e}\n")

    # Persistent path: scan all mounted dataset dirs.
    datasets_dir = Path(os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data"))
    if datasets_dir.exists():
        for ds_dir in datasets_dir.iterdir():
            candidate = ds_dir / "_dd_backup_config.json"
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text())
                if data.get("db_id") == db_id and data.get("snapshot_dir"):
                    cfg = dict(cfg)
                    cfg["snapshot_dir"] = data["snapshot_dir"]
                    # Warm the in-memory cache for sibling processes.
                    try:
                        _BACKUP_OVERRIDE.write_text(json.dumps(data))
                    except Exception:
                        pass
                    sys.stderr.write(
                        f"[lifecycle] backup config loaded from {candidate}: "
                        f"snapshot_dir={cfg['snapshot_dir']}\n"
                    )
                    return cfg
            except Exception as e:
                sys.stderr.write(f"[lifecycle] skipping {candidate}: {e}\n")
    return cfg


def save_backup_config(cfg: dict, snapshot_dir: Path) -> None:
    """Persist the backup location to both the in-memory and on-disk stores."""
    db_id = cfg.get("db_id", "")
    data = {"db_id": db_id, "snapshot_dir": str(snapshot_dir)}

    # In-memory (picked up by snapshotters on next tick without restart).
    try:
        _BACKUP_OVERRIDE.write_text(json.dumps(data))
    except Exception as e:
        sys.stderr.write(f"[lifecycle] could not write {_BACKUP_OVERRIDE}: {e}\n")

    # Persistent (survives container restart).
    try:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        persistent = snapshot_dir / "_dd_backup_config.json"
        persistent.write_text(json.dumps({**data, "configured_at": __import__("datetime").datetime.utcnow().isoformat() + "Z"}))
        sys.stderr.write(f"[lifecycle] backup config saved to {persistent}\n")
    except Exception as e:
        sys.stderr.write(f"[lifecycle] could not write persistent backup config: {e}\n")


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
        # Postgres refuses to start unless the data dir is 0700 or 0750.
        # mkdir(parents=True) above left it 0755; initdb-fresh-path chmods
        # to 0700 implicitly, but tar-restore doesn't touch the containing
        # dir's mode.
        pgdata.chmod(0o700)
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
            pgdata.chmod(0o700)  # Postgres rejects 0755
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
    log_path = "/var/log/dd/postgres.log"
    result = subprocess.run([
        PGCTL, "-D", pgdata, "-l", log_path,
        "-o", f"-p {port}", "start",
    ])
    if result.returncode != 0:
        # Dump the postgres log so we don't have to ssh in to debug.
        sys.stderr.write(
            f"[lifecycle] pg_ctl start failed (rc={result.returncode}). "
            f"postgres.log tail:\n"
        )
        from dbapp.engines._common import redact
        try:
            with open(log_path) as f:
                sys.stderr.write(redact(f.read()[-3000:]))
        except OSError as e:
            sys.stderr.write(f"  (could not read {log_path}: {e})\n")
        sys.stderr.write(f"\n[lifecycle] pgdata listing:\n")
        try:
            for p in sorted(Path(pgdata).iterdir()):
                sys.stderr.write(f"  {p.name}\n")
        except OSError as e:
            sys.stderr.write(f"  (could not list {pgdata}: {e})\n")
        raise RuntimeError(f"Postgres failed to start (pg_ctl rc={result.returncode})")
    # Wait for readiness
    for _ in range(30):
        r = subprocess.run(["pg_isready", "-h", "127.0.0.1", "-p", str(port), "-q"])
        if r.returncode == 0:
            return
        time.sleep(1)
    # Same log-dump on timeout.
    from dbapp.engines._common import redact
    try:
        with open(log_path) as f:
            sys.stderr.write(f"[lifecycle] pg_isready timeout. postgres.log tail:\n{redact(f.read()[-3000:])}\n")
    except OSError:
        pass
    raise RuntimeError("Postgres failed to become ready in 30s")


PGBOUNCER_PORT = 6432


def start_pgbouncer(cfg: dict) -> int | None:
    """Run pgbouncer in front of Postgres for connection pooling.

    Why: every new client connection through the /wire WebSocket relay
    pays Postgres's per-connection startup cost (auth, role lookup,
    process fork). Tools that open a new connection per query (ORMs,
    notebooks with `with engine.connect():` patterns) become slow.
    pgbouncer keeps ~25 backend connections warm and multiplexes hundreds
    of client connections onto them.

    Returns the port pgbouncer is listening on (6432), or None if
    pgbouncer isn't installed in the env image — the router then falls
    back to talking to Postgres directly.
    """
    import shutil as _sh
    if not _sh.which("pgbouncer"):
        sys.stderr.write("[lifecycle] pgbouncer not installed — clients hit Postgres directly\n")
        return None

    pg_port = cfg.get("port", 5432)
    user = cfg.get("user", "domino")

    # Fetch the SCRAM-SHA-256 password hash that Postgres stored for our
    # user. pgbouncer can't validate scram passwords without the hash,
    # and we don't want to hash it ourselves (Postgres's scram salting
    # is non-trivial to replicate from Python).
    psql_bin = "/usr/lib/postgresql/16/bin/psql"
    socket_dir = cfg.get("socket_dir", "/mnt/db/sock")
    hash_proc = subprocess.run(
        [psql_bin, "-h", socket_dir, "-p", str(pg_port), "-U", user,
         "-d", "postgres", "-Atqc",
         f"SELECT passwd FROM pg_shadow WHERE usename = '{user}'"],
        capture_output=True, text=True,
    )
    if hash_proc.returncode != 0 or "SCRAM-SHA-256" not in hash_proc.stdout:
        sys.stderr.write(f"[lifecycle] couldn't fetch SCRAM hash for {user}: {hash_proc.stderr}\n")
        return None
    scram_hash = hash_proc.stdout.strip()

    pb_dir = Path("/mnt/db/pgbouncer")
    pb_dir.mkdir(parents=True, exist_ok=True)
    userlist = pb_dir / "userlist.txt"
    userlist.write_text(f'"{user}" "{scram_hash}"\n')
    userlist.chmod(0o600)

    config = pb_dir / "pgbouncer.ini"
    config.write_text(f"""[databases]
* = host=127.0.0.1 port={pg_port}

[pgbouncer]
listen_addr = 127.0.0.1
listen_port = {PGBOUNCER_PORT}
unix_socket_dir =
auth_type = scram-sha-256
auth_file = {userlist}
pool_mode = transaction
max_client_conn = 500
default_pool_size = 25
reserve_pool_size = 5
server_lifetime = 3600
server_idle_timeout = 600
log_connections = 0
log_disconnections = 0
logfile = /var/log/dd/pgbouncer.log
pidfile = {pb_dir / "pgbouncer.pid"}
""")

    # Start pgbouncer detached (it daemonizes itself with -d, but writes its
    # PID file; if a previous instance left one behind, remove it).
    pid_file = pb_dir / "pgbouncer.pid"
    if pid_file.exists():
        try: pid_file.unlink()
        except OSError: pass
    subprocess.Popen(
        ["pgbouncer", "-d", str(config)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Wait for it to bind
    for _ in range(20):
        try:
            s = socket.create_connection(("127.0.0.1", PGBOUNCER_PORT), timeout=0.5)
            s.close()
            sys.stderr.write(
                f"[lifecycle] pgbouncer up on :{PGBOUNCER_PORT} → postgres :{pg_port} "
                f"(pool_mode=transaction, default_pool_size=25)\n"
            )
            return PGBOUNCER_PORT
        except OSError:
            time.sleep(0.3)
    sys.stderr.write("[lifecycle] pgbouncer failed to bind — falling back to direct postgres\n")
    return None


# Mongo / MySQL / Redis are owned end-to-end by their EngineAdapter
# subclasses under dbapp/engines/. Postgres has its helpers above for
# historical reasons (the adapter delegates back to them); the other
# engines never split.


# --------------------------------------------------------------------------
# Admin UI launcher — engine-agnostic. Each EngineAdapter returns an
# AdminUISpec describing argv + env + internal port; we Popen it once at
# boot, the Flask router proxies /admin/ to internal_port.
# --------------------------------------------------------------------------
def start_admin_ui(cfg: dict, adapter) -> int | None:
    """Launch the adapter's admin UI process. Returns the internal port
    the router should reverse-proxy /admin/ to, or None if this engine
    ships no UI."""
    spec = adapter.admin_ui_spec(cfg)
    if spec is None:
        sys.stderr.write(f"[lifecycle] no admin UI for engine={adapter.name}\n")
        return None
    Path("/var/log/dd").mkdir(parents=True, exist_ok=True)
    log_handle = open(spec.log_path, "a")
    env = os.environ.copy()
    env.update(spec.env)
    sys.stderr.write(
        f"[lifecycle] launching admin UI: {spec.argv[0]} on :{spec.internal_port}\n"
    )
    subprocess.Popen(
        spec.argv,
        env=env,
        stdout=log_handle, stderr=log_handle,
        start_new_session=True, close_fds=True,
    )
    return spec.internal_port


# Kept for backward compatibility — early tests still import this name.
def start_pgweb(cfg: dict) -> None:
    if cfg["engine"] != "postgres":
        return
    from dbapp import engines
    start_admin_ui(cfg, engines.get("postgres"))


# --------------------------------------------------------------------------
# Cron snapshotter
# --------------------------------------------------------------------------
def schedule_snapshotter(cfg: dict, adapter=None) -> None:
    """Spawn a detached subprocess that runs the engine's snapshotter on
    a fixed interval. cron-as-ubuntu was the previous mechanism but never
    fired without sudoers configuration; a self-contained shell loop in a
    new session is root-free and survives the parent's exec to gunicorn.

    Engine-agnostic via the adapter: we read script name + env vars from
    adapter.snapshot_script_name() and adapter.snapshot_env(cfg).
    """
    if adapter is None:
        from dbapp import engines
        adapter = engines.get(cfg["engine"])

    interval_min = int(cfg.get("snapshot_interval_min", 60))
    script_name = adapter.snapshot_script_name()
    # Prefer /mnt/code/snapshotter/ (live from this commit) over the baked
    # /opt/dd/snapshotter/ — same dev-iteration preference as the dispatcher
    # picks /mnt/code/dbapp/ over /opt/dd/.
    script = None
    for candidate in (
        f"/mnt/code/snapshotter/{script_name}",
        f"/opt/dd/snapshotter/{script_name}",
    ):
        if Path(candidate).exists():
            script = candidate
            break
    if not script:
        sys.stderr.write(f"[lifecycle] WARN: snapshotter {script_name} not found\n")
        return

    snap_dir = snapshot_path(cfg)
    env = os.environ.copy()
    env.update({
        "DD_DB_ID": cfg["db_id"],
        "DD_SNAPSHOT_DIR": str(snap_dir),
    })
    env.update(adapter.snapshot_env(cfg))

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
            start_new_session=True,
            close_fds=True,
        )
    sys.stderr.write(
        f"[lifecycle] snapshotter loop started pid={proc.pid} "
        f"(engine={adapter.name}, every {interval_sec}s, script={script})\n"
    )

    # Stage a ready-to-run script for the SIGTERM trap in dbapp/app.sh
    # to call on graceful shutdown — captures the last write before the
    # container disappears. All env baked in so it runs without parent.
    final_helper = Path("/tmp/dd-final-snapshot.sh")
    env_exports = "\n".join(
        f"export {k}={v!r}" for k, v in {
            "DD_DB_ID": cfg["db_id"],
            "DD_SNAPSHOT_DIR": str(snap_dir),
            **adapter.snapshot_env(cfg),
            "DOMINO_API_PROXY": os.environ.get("DOMINO_API_PROXY", "http://localhost:8899"),
            "DOMINO_USER_API_KEY": os.environ.get("DOMINO_USER_API_KEY", ""),
            "DOMINO_PROJECT_ID": os.environ.get("DOMINO_PROJECT_ID", ""),
            "DOMINO_DATASETS_DIR": os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data"),
            "DOMINO_PROJECT_NAME": os.environ.get("DOMINO_PROJECT_NAME", "default"),
        }.items()
    )
    final_helper.write_text(
        "#!/bin/bash\n"
        "# Run by dbapp/app.sh on SIGTERM/SIGINT. Bake a fresh snapshot\n"
        "# before the container is killed.\n"
        f"{env_exports}\n"
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
    cfg = load_backup_override(cfg)
    engine_name = cfg["engine"]
    sys.stderr.write(f"[lifecycle] booting engine={engine_name} db_id={cfg['db_id']}\n")

    # Resolve engine adapter — this is the only place we case-by-engine.
    from dbapp import engines
    try:
        adapter = engines.get(engine_name)
    except KeyError as e:
        raise RuntimeError(f"unsupported engine: {engine_name} ({e})") from e

    # The adapter owns restore-or-init, start, and optional client-port
    # (e.g. pgbouncer for Postgres). client_port=None means "use cfg port".
    adapter.restore_or_init(cfg)
    client_port = adapter.start(cfg)
    if client_port:
        cfg["client_port"] = client_port

    # Launch the per-engine admin UI (pgweb / mongo-express / phpMyAdmin /
    # redis-commander). Records admin_port for the router to proxy to.
    admin_port = start_admin_ui(cfg, adapter)
    if admin_port:
        cfg["admin_port"] = admin_port

    schedule_snapshotter(cfg, adapter)
    sys.stderr.write(f"[lifecycle] all sidecars launched (engine={engine_name})\n")
    return cfg


if __name__ == "__main__":
    cfg = boot()
    print(json.dumps({"booted": True, "engine": cfg["engine"], "db_id": cfg["db_id"]}))
