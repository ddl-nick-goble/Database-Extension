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

def _default_dbapps_dir() -> Path:
    """Where per-DB config files live. Default is a subdir of the project's
    default dataset so any project using dd-postgres-app can read configs
    without depending on /mnt/code being this repo."""
    base = os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data")
    project = os.environ.get("DOMINO_PROJECT_NAME", "default")
    return Path(base) / project / "_dd_configs"


DBAPPS_DIR = Path(os.environ.get("DD_DBAPPS_DIR")) if os.environ.get("DD_DBAPPS_DIR") else _default_dbapps_dir()


def find_config() -> dict:
    """Locate this App's config. Resolution order:

      1. $DD_CONFIG_JSON inline JSON blob          (self-contained apps — primary path
                                                    for apps created in any project)
      2. $DD_CONFIG explicit file path             (test / manual override)
      3. $DOMINO_APP_NAME.json                     (not present on Domino Apps)
      4. Domino API lookup by $DOMINO_RUN_ID       (same-project fallback)
      5. Most recent .json filtered by $DD_ENGINE  (defensive last resort)

    Why (1) first: DB apps are now created with entryPoint=/opt/dd/app.sh and
    their config passed as DD_CONFIG_JSON in environmentVariables at start
    time.  This makes them fully project-independent — no file needs to exist
    in the project dataset.  The wizard still writes a .json file as a
    belt-and-suspenders fallback for (4)/(5).

    Why (4): Domino App containers don't receive DOMINO_APP_NAME, but they
    DO get DOMINO_RUN_ID (the instance id) and DOMINO_USER_API_KEY.

    Why (5) is filtered by engine: silently loading a Mongo config into a
    Postgres container crashes on engine-specific paths.  DD_ENGINE is baked
    into every env image so it's the safest disambiguator.
    """
    # Build marker — grep the boot log for this to confirm WHICH lifecycle the
    # /opt/dd image is running. The project-env channel only exists in builds
    # that print this line; an old image won't, and will instead log the
    # "cannot import domino_api" message. If you see neither, the env image
    # predates this fix and needs a rebuild.
    sys.stderr.write(
        "[lifecycle] find_config build=projenv-v1 "
        "channels=[inline, DD_CONFIG, project-env(DD_CFG_<app_id>), "
        "DOMINO_APP_NAME, run-id-file, engine-mtime]\n"
    )
    inline = os.environ.get("DD_CONFIG_JSON", "").strip()
    if inline:
        try:
            cfg = json.loads(inline)
            sys.stderr.write("[lifecycle] config from DD_CONFIG_JSON env var\n")
            return cfg
        except json.JSONDecodeError as e:
            sys.stderr.write(f"[lifecycle] DD_CONFIG_JSON is set but invalid JSON ({e}) — falling through\n")

    explicit = os.environ.get("DD_CONFIG")
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise RuntimeError(f"DD_CONFIG={explicit} but file does not exist")
        sys.stderr.write(f"[lifecycle] config from DD_CONFIG={p}\n")
        return json.loads(p.read_text())

    # Project-env channel: the wizard stashes the config (base64 JSON) as a
    # project env var DD_CFG_<app_id>. Project env vars are reliably injected
    # into the App's container — unlike /mnt/code writes (wiped by the run
    # container's fresh git checkout) or version preRunScript/env (silently
    # dropped on some builds). We resolve our own app_id from DOMINO_RUN_ID via
    # a direct proxy call (no domino_api import — that module isn't present in
    # arbitrary projects, which is why the old API path failed).
    run_id = os.environ.get("DOMINO_RUN_ID", "")
    if run_id:
        cfg = _config_from_project_env(run_id)
        if cfg is not None:
            return cfg

    app_name = os.environ.get("DOMINO_APP_NAME", "")
    if app_name:
        p = DBAPPS_DIR / f"{app_name}.json"
        if p.exists():
            sys.stderr.write(f"[lifecycle] config from DOMINO_APP_NAME={app_name}\n")
            return json.loads(p.read_text())
        sys.stderr.write(
            f"[lifecycle] DOMINO_APP_NAME={app_name} but {p} missing — trying API lookup\n"
        )

    run_id = os.environ.get("DOMINO_RUN_ID", "")
    if run_id:
        resolved = _resolve_app_name_from_run_id(run_id)
        if resolved:
            p = DBAPPS_DIR / f"{resolved}.json"
            if p.exists():
                sys.stderr.write(
                    f"[lifecycle] config matched via DOMINO_RUN_ID={run_id} → {resolved}.json\n"
                )
                return json.loads(p.read_text())
            sys.stderr.write(
                f"[lifecycle] API resolved run_id={run_id} → name={resolved}, but {p} missing\n"
            )

    engine = os.environ.get("DD_ENGINE", "").strip()
    search_dirs = [DBAPPS_DIR, Path("/mnt/code/dbapps")]
    candidates: list[Path] = []
    for d in search_dirs:
        if d.exists():
            candidates.extend(d.glob("*.json"))
    if engine and candidates:
        # Only consider configs that match this container's engine — silently
        # booting a Mongo config in a Postgres container is the worst-case bug.
        filtered: list[Path] = []
        for c in candidates:
            try:
                if json.loads(c.read_text()).get("engine") == engine:
                    filtered.append(c)
            except Exception:
                continue
        if not filtered:
            sys.stderr.write(_resolution_diagnostics(search_dirs, engine) + "\n")
            raise RuntimeError(
                f"No config file with engine={engine!r} found in "
                f"{[str(d) for d in search_dirs]}, and the project-env channel "
                f"(DD_CFG_<app_id>) did not resolve. See the diagnostics above. "
                f"Most likely: the env image predates the project-env fix "
                f"(rebuild dd-{engine}-app), or the wizard couldn't set the "
                f"config env var on this project."
            )
        candidates = filtered
    candidates = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        sys.stderr.write(_resolution_diagnostics(search_dirs, engine) + "\n")
        raise RuntimeError(
            f"No config found. The project-env channel (DD_CFG_<app_id>) did "
            f"not resolve and no config file exists in "
            f"{[str(d) for d in search_dirs]}. See the diagnostics above. Most "
            f"likely: the env image predates the project-env fix (rebuild the "
            f"dd-<engine>-app environment), or the wizard couldn't set the "
            f"config env var on this project."
        )
    sys.stderr.write(
        f"[lifecycle] config from mtime fallback "
        f"(engine_filter={engine or '<none>'}): {candidates[0]}\n"
    )
    return json.loads(candidates[0].read_text())


def _fetch_apps_via_api() -> list[dict]:
    """List this project's Apps via a direct call to the in-pod auth proxy.

    Self-contained on purpose: uses `requests` (baked into every DB image) and
    the standard DOMINO_* env vars, NOT the project's domino_api.py — that
    module only exists in the Database-Extension repo, so importing it failed
    in every other project (the "No module named 'domino_api'" you saw).
    """
    proxy = os.environ.get("DOMINO_API_PROXY", "http://localhost:8899")
    api_key = os.environ.get("DOMINO_USER_API_KEY", "")
    project_id = os.environ.get("DOMINO_PROJECT_ID", "")
    if not (api_key and project_id):
        sys.stderr.write("[lifecycle] no API key / project id — skipping API lookup\n")
        return []
    try:
        import requests
        r = requests.get(
            f"{proxy}/v4/modelProducts",
            params={"projectId": project_id},
            headers={"X-Domino-Api-Key": api_key},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        sys.stderr.write(f"[lifecycle] app list via API failed: {e}\n")
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "items", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def _app_from_run_id(run_id: str) -> dict | None:
    """Return the App dict whose current instance id matches `run_id`, or None."""
    for a in _fetch_apps_via_api():
        ci = (a.get("currentVersion") or {}).get("currentInstance") or {}
        if ci.get("id") == run_id:
            return a
    return None


def _config_from_project_env(run_id: str) -> dict | None:
    """Resolve our app_id from `run_id`, then decode the base64 config the
    wizard stashed as project env var DD_CFG_<app_id>. Returns None if the app
    can't be resolved or no such var is set (caller falls through)."""
    import base64
    app = _app_from_run_id(run_id)
    if not app:
        return None
    app_id = app.get("id", "")
    raw = os.environ.get(f"DD_CFG_{app_id.upper()}", "") if app_id else ""
    if not raw:
        return None
    try:
        cfg = json.loads(base64.b64decode(raw).decode())
        sys.stderr.write(f"[lifecycle] config from project env DD_CFG_{app_id}\n")
        return cfg
    except Exception as e:
        sys.stderr.write(f"[lifecycle] DD_CFG_{app_id} present but undecodable: {e}\n")
        return None


def _resolve_app_name_from_run_id(run_id: str) -> str | None:
    """Return the App *name* whose current instance id matches `run_id`, for the
    file-based config fallback. Self-contained (see _fetch_apps_via_api)."""
    app = _app_from_run_id(run_id)
    return app.get("name") if app else None


def _resolution_diagnostics(search_dirs: list[Path], engine: str) -> str:
    """Human-readable dump of every signal find_config used, written to the boot
    log right before we give up. Tells you exactly which channel fell short
    without needing to shell into a dead container.

    Never prints secret VALUES — only presence/booleans and var NAMES.
    """
    run_id = os.environ.get("DOMINO_RUN_ID", "")
    L = ["[lifecycle] ---- config resolution diagnostics ----"]
    L.append(
        f"  env: DD_ENGINE={engine or '<unset>'} "
        f"DOMINO_PROJECT_NAME={os.environ.get('DOMINO_PROJECT_NAME', '<unset>')} "
        f"DOMINO_DATASETS_DIR={os.environ.get('DOMINO_DATASETS_DIR', '<unset>')}"
    )
    L.append(
        f"  ids: DOMINO_RUN_ID={run_id or '<unset>'} "
        f"DOMINO_PROJECT_ID={'set' if os.environ.get('DOMINO_PROJECT_ID') else '<unset>'} "
        f"DOMINO_USER_API_KEY={'set' if os.environ.get('DOMINO_USER_API_KEY') else '<unset>'} "
        f"DOMINO_API_PROXY={os.environ.get('DOMINO_API_PROXY', '<unset>')}"
    )
    cfg_vars = sorted(k for k in os.environ if k.startswith("DD_CFG_"))
    L.append(f"  project-env: DD_CFG_* vars present={cfg_vars or '<none>'}")
    if run_id:
        try:
            apps = _fetch_apps_via_api()
            match = _app_from_run_id(run_id)
            L.append(
                f"  project-env: API returned {len(apps)} app(s); "
                f"run_id matched app_id={(match or {}).get('id', '<no match>')}"
            )
            if match:
                want = f"DD_CFG_{(match.get('id') or '').upper()}"
                L.append(
                    f"  project-env: expected var {want} "
                    f"{'PRESENT' if os.environ.get(want) else 'MISSING — wizard did not set it'}"
                )
        except Exception as e:  # diagnostics must never mask the real error
            L.append(f"  project-env: API probe errored: {e}")
    else:
        L.append("  project-env: DOMINO_RUN_ID unset — cannot resolve app_id")
    for d in search_dirs:
        if d.exists():
            jsons = [p.name for p in d.glob("*.json")]
            L.append(f"  file: {d} exists, *.json={jsons or '<none>'}")
        else:
            L.append(f"  file: {d} MISSING")
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
