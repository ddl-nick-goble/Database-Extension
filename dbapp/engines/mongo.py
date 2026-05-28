"""MongoDB engine adapter.

Runs mongod as a single-node replica set ('rs0') so mongodump --oplog
produces a consistent snapshot — without a replSet, --oplog refuses to
run, and we lose point-in-time recovery on the snapshot path.

Explorer: mongo-express at /admin/, MIT-licensed Node app, pre-connected
to the local mongod with cfg credentials.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import EngineAdapter, AdminUISpec, ConnectionSnippet, _register
from . import _common

_MONGO_LOG = "/var/log/dd/mongod.log"
_MONGO_PIDFILE = "/mnt/db/mongo.pid"


class MongoAdapter(EngineAdapter):
    name = "mongo"
    docs_label = "MongoDB"
    icon = ""
    description = "Document database for JSON-shaped data"
    default_port = 27017
    app_prefix = "mongo-"
    env_id_var = "DD_MONGO_ENV_ID"

    # ----- lifecycle -----
    def restore_or_init(self, cfg: dict) -> str:
        data = _common.data_path(cfg, "mongo")
        data.mkdir(parents=True, exist_ok=True)
        snap_root = _common.snapshot_path(cfg)
        snap_root.mkdir(parents=True, exist_ok=True)

        # Already-populated data dir = warm restart, no setup needed.
        if any(data.iterdir()):
            return "noop"

        snapshots = snap_root / "snapshots"
        if snapshots.exists() and any(snapshots.iterdir()):
            return "restore"
        return "fresh"

    def start(self, cfg: dict) -> int | None:
        data = _common.data_path(cfg, "mongo")
        port = int(cfg.get("port", self.default_port))
        state = self.restore_or_init(cfg)

        Path("/var/log/dd").mkdir(parents=True, exist_ok=True)

        # Phase 1: start mongod WITHOUT auth so we can create the admin
        # user (fresh path) or initiate the replSet. Auth is enabled in
        # phase 2 with a restart.
        sys.stderr.write(f"[mongo] phase 1: starting mongod (no-auth, replSet=rs0)\n")
        self._launch(
            data=data, port=port, with_auth=False,
            extra=["--replSet", "rs0", "--bind_ip", "127.0.0.1"],
        )
        if not _common.wait_for_port(port, timeout_s=30):
            _common.dump_log_tail(_MONGO_LOG, "mongo")
            raise RuntimeError("mongod failed to bind in 30s (phase 1)")

        if not _common.wait_for_check(
            lambda: self._ping(port, with_auth=False), timeout_s=20,
        ):
            _common.dump_log_tail(_MONGO_LOG, "mongo")
            raise RuntimeError("mongosh ping never succeeded (phase 1)")

        # Initiate the replSet (idempotent — replays are no-ops post-init).
        self._mongosh_eval(port, """
try { rs.initiate({_id:"rs0", members:[{_id:0, host:"127.0.0.1:%d"}]}); }
catch(e) { print("rs.initiate noop: " + e.message); }
""" % port)
        # Wait until we're PRIMARY — required before createUser.
        if not _common.wait_for_check(
            lambda: self._is_primary(port), timeout_s=30,
        ):
            raise RuntimeError("mongod never became PRIMARY")

        if state == "fresh":
            sys.stderr.write("[mongo] creating root user\n")
            self._create_user(port, cfg)
        elif state == "restore":
            self._mongorestore(port, cfg)

        # Phase 2: restart mongod WITH auth + keyFile (single-node replSet
        # needs a keyFile if auth is on). Use the password as keyFile
        # contents — single node, no real cross-node auth surface.
        sys.stderr.write("[mongo] phase 2: restarting mongod with auth\n")
        self._stop(port)
        keyfile = Path("/mnt/db/mongo.keyfile")
        if not keyfile.exists():
            keyfile.write_text(cfg["password"] + "\n")
            keyfile.chmod(0o600)
        self._launch(
            data=data, port=port, with_auth=True,
            extra=["--replSet", "rs0", "--bind_ip", "127.0.0.1",
                   "--keyFile", str(keyfile)],
        )
        if not _common.wait_for_port(port, timeout_s=30):
            _common.dump_log_tail(_MONGO_LOG, "mongo")
            raise RuntimeError("mongod failed to bind in 30s (phase 2)")
        return None  # no pooler — clients talk directly to mongod

    def shutdown(self, cfg: dict) -> None:
        port = int(cfg.get("port", self.default_port))
        try:
            self._stop(port)
        except Exception as e:
            sys.stderr.write(f"[mongo.shutdown] {e}\n")

    def health_check(self, cfg: dict) -> bool:
        port = int(cfg.get("port", self.default_port))
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            return False

    # ----- admin UI: mongo-express -----
    def admin_ui_spec(self, cfg: dict) -> AdminUISpec | None:
        # mongo-express is npm-installed globally in the env image. If a
        # site-local install is used instead, ME_BIN can override the path.
        me_bin = os.environ.get("ME_BIN", "mongo-express")
        port = int(cfg.get("admin_port", 8978))
        mongo_port = int(cfg.get("port", self.default_port))
        user = cfg.get("user", self.default_user)
        pw = cfg["password"]
        return AdminUISpec(
            argv=[me_bin],
            internal_port=port,
            env={
                # https://github.com/mongo-express/mongo-express#usage
                "ME_CONFIG_MONGODB_URL": (
                    f"mongodb://{user}:{pw}@127.0.0.1:{mongo_port}/"
                    f"?authSource=admin&directConnection=true"
                ),
                "ME_CONFIG_SITE_BASEURL": "/admin/",
                # mongo-express 1.x dropped the 0.x `ME_CONFIG_BASICAUTH`
                # toggle and instead derives `useBasicAuth` from
                # `ME_CONFIG_BASICAUTH_USERNAME` being non-empty. It also
                # requires cookie + session secrets at startup or it exits
                # before binding to PORT. Reuse the DB creds — the Domino
                # app proxy is the real gate; mongo-express's basic auth
                # is just a belt-and-suspenders backstop.
                "ME_CONFIG_BASICAUTH_USERNAME": user,
                "ME_CONFIG_BASICAUTH_PASSWORD": pw,
                "ME_CONFIG_SITE_COOKIESECRET": pw,
                "ME_CONFIG_SITE_SESSIONSECRET": pw,
                "PORT": str(port),
                "VCAP_APP_HOST": "127.0.0.1",
                "VCAP_APP_PORT": str(port),
            },
            log_path="/var/log/dd/mongo-express.log",
            prefix_aware=True,
        )

    # ----- connection snippets -----
    def connection_strings(self, cfg: dict, client_port: int) -> list[ConnectionSnippet]:
        user = cfg.get("user", self.default_user)
        return [
            ConnectionSnippet(
                label="mongosh",
                snippet=(
                    f'mongosh "mongodb://{user}@127.0.0.1:{client_port}/admin'
                    f'?directConnection=true"'
                ),
                note="directConnection=true required because we run a single-node replSet",
            ),
        ]

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _launch(self, data: Path, port: int, with_auth: bool, extra: list[str]) -> None:
        cmd = [
            "mongod",
            "--dbpath", str(data),
            "--port", str(port),
            "--logpath", _MONGO_LOG,
            "--pidfilepath", _MONGO_PIDFILE,
            "--fork",
            # Cap WiredTiger cache — bank-playground tier, not OLTP.
            # Default heuristic (50% of RAM) is too aggressive on small
            # Domino hardware tiers and starves the rest of the pod.
            "--wiredTigerCacheSizeGB", "0.5",
        ]
        if with_auth:
            cmd.append("--auth")
        cmd.extend(extra)
        subprocess.run(cmd, check=True)

    def _stop(self, port: int) -> None:
        # Try a clean shutdown via mongosh, then fall back to SIGTERM via
        # the pid file. mongod's shutdown command requires the admin user
        # when auth is enabled, so we send creds via the env var.
        try:
            subprocess.run(
                ["mongosh", "--quiet", "--port", str(port), "admin",
                 "--eval", "db.shutdownServer({force:true})"],
                timeout=15, check=False,
            )
        except Exception:
            pass
        pid_path = Path(_MONGO_PIDFILE)
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                os.kill(pid, 15)
                time.sleep(2)
            except (OSError, ValueError):
                pass

    def _ping(self, port: int, with_auth: bool) -> bool:
        r = subprocess.run(
            ["mongosh", "--quiet", "--port", str(port),
             "--eval", "db.runCommand({ping:1}).ok"],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0 and "1" in r.stdout

    def _is_primary(self, port: int) -> bool:
        r = subprocess.run(
            ["mongosh", "--quiet", "--port", str(port),
             "--eval", "rs.status().myState"],
            capture_output=True, text=True, timeout=10,
        )
        # Mongo state 1 = PRIMARY
        return r.returncode == 0 and r.stdout.strip().endswith("1")

    def _create_user(self, port: int, cfg: dict) -> None:
        user = cfg.get("user", self.default_user)
        pw = cfg["password"]
        # JSON-encode the password so embedded quotes are safe.
        script = (
            'db.createUser({user:%s, pwd:%s, '
            'roles:[{role:"root", db:"admin"}]})'
            % (json.dumps(user), json.dumps(pw))
        )
        r = subprocess.run(
            ["mongosh", "--quiet", "--port", str(port), "admin",
             "--eval", script],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"createUser failed: {_common.redact(r.stderr)[:500]}"
            )

    def _mongorestore(self, port: int, cfg: dict) -> None:
        snap_root = _common.snapshot_path(cfg)
        snapshots = snap_root / "snapshots"
        latest = sorted(snapshots.iterdir(), key=lambda p: p.name)[-1]
        sys.stderr.write(f"[mongo] mongorestore from {latest.name}\n")
        # Restore runs BEFORE auth is enabled — no creds needed.
        subprocess.run(
            ["mongorestore", "--port", str(port),
             "--gzip", "--oplogReplay", str(latest)],
            check=True,
        )

    def _mongosh_eval(self, port: int, script: str) -> None:
        subprocess.run(
            ["mongosh", "--quiet", "--port", str(port), "--eval", script],
            check=False, timeout=20,
        )

    def snapshot_env(self, cfg: dict) -> dict[str, str]:
        return {
            "DD_MONGO_PORT": str(cfg.get("port", self.default_port)),
            "DD_MONGO_USER": cfg.get("user", self.default_user),
            "DD_MONGO_PASSWORD": cfg["password"],
        }


_register(MongoAdapter())
