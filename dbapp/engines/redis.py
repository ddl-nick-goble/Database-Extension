"""Redis 7.x engine adapter.

Persistence: AOF (`appendfsync everysec`) + RDB snapshot dumps. The
snapshotter copies dump.rdb to a Domino dataset for versioned restore;
the live AOF protects against crash-window loss.

Explorer: redis-commander (Node, MIT), pre-connected to the local redis
with cfg credentials via env vars (not CLI flags — keeps password out
of /proc/<pid>/cmdline).
"""

from __future__ import annotations

import gzip
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import EngineAdapter, AdminUISpec, ConnectionSnippet, _register
from . import _common

_REDIS_LOG = "/var/log/dd/redis.log"
_REDIS_CONF = "/etc/dd-redis.conf"
_REDIS_PIDFILE = "/mnt/db/redis.pid"


class RedisAdapter(EngineAdapter):
    name = "redis"
    docs_label = "Redis"
    icon = ""
    description = "Fast in-memory key-value store"
    default_port = 6379
    app_prefix = "redis-"
    env_id_var = "DD_REDIS_ENV_ID"

    # ----- lifecycle -----
    def restore_or_init(self, cfg: dict) -> str:
        data = _common.data_path(cfg, "redis")
        data.mkdir(parents=True, exist_ok=True)
        snap_root = _common.snapshot_path(cfg)
        snap_root.mkdir(parents=True, exist_ok=True)

        # Already-populated data dir (AOF or RDB present) = warm restart.
        if (data / "dump.rdb").exists() or (data / "appendonlydir").exists():
            return "noop"

        latest = snap_root / "snapshots" / "latest" / "dump.rdb.gz"
        if latest.exists():
            sys.stderr.write(f"[redis] restoring dump.rdb from {latest}\n")
            with gzip.open(latest, "rb") as src, open(data / "dump.rdb", "wb") as dst:
                shutil.copyfileobj(src, dst)
            return "restore"

        return "fresh"

    def start(self, cfg: dict) -> int | None:
        data = _common.data_path(cfg, "redis")
        port = int(cfg.get("port", self.default_port))
        # restore_or_init populates dump.rdb (or doesn't) before redis-server
        # starts; redis-server loads it implicitly on launch.
        self.restore_or_init(cfg)

        Path("/var/log/dd").mkdir(parents=True, exist_ok=True)
        self._write_conf(cfg, port, data)
        self._launch()
        if not _common.wait_for_check(
            lambda: self._ping(port, cfg["password"]), timeout_s=30,
        ):
            _common.dump_log_tail(_REDIS_LOG, "redis")
            raise RuntimeError("redis-server failed to PING in 30s")
        return None

    def shutdown(self, cfg: dict) -> None:
        port = int(cfg.get("port", self.default_port))
        try:
            subprocess.run(
                ["redis-cli", "-h", "127.0.0.1", "-p", str(port),
                 "-a", cfg["password"], "--no-auth-warning",
                 "SHUTDOWN", "SAVE"],
                timeout=20, check=False,
            )
        except Exception as e:
            sys.stderr.write(f"[redis.shutdown] {e}\n")

    def health_check(self, cfg: dict) -> bool:
        port = int(cfg.get("port", self.default_port))
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            return False

    # ----- admin UI: redis-commander -----
    def admin_ui_spec(self, cfg: dict) -> AdminUISpec | None:
        rc_bin = os.environ.get("RC_BIN", "redis-commander")
        port = int(cfg.get("admin_port", 8978))
        redis_port = int(cfg.get("port", self.default_port))
        pw = cfg["password"]
        return AdminUISpec(
            # No --redis-password on argv — pass via env so it doesn't
            # show up in /proc/<pid>/cmdline. redis-commander reads
            # REDIS_PASSWORD from the env automatically.
            argv=[
                rc_bin,
                "--port", str(port),
                "--address", "127.0.0.1",
                "--url-prefix", "/admin",
                "--redis-host", "127.0.0.1",
                "--redis-port", str(redis_port),
                "--no-log-data",
                "--noauth",
            ],
            internal_port=port,
            env={
                # redis-commander recognizes REDIS_PASSWORD for the
                # default connection.
                "REDIS_PASSWORD": pw,
            },
            log_path="/var/log/dd/redis-commander.log",
            prefix_aware=True,
        )

    # ----- connection snippets -----
    def connection_strings(self, cfg: dict, client_port: int) -> list[ConnectionSnippet]:
        return [
            ConnectionSnippet(
                label="redis-cli",
                snippet=(
                    f"redis-cli -h 127.0.0.1 -p {client_port} "
                    f"-a <password> --no-auth-warning"
                ),
            ),
            ConnectionSnippet(
                label="connection URL",
                snippet=f"redis://default:<password>@127.0.0.1:{client_port}/0",
            ),
        ]

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _write_conf(self, cfg: dict, port: int, data: Path) -> None:
        pw = cfg["password"]
        maxmem = cfg.get("redis_maxmemory", "1gb")
        Path(_REDIS_CONF).write_text(
            f"""# Generated by dbapp.engines.redis — do not edit by hand
bind 127.0.0.1
port {port}
protected-mode yes
requirepass {pw}
dir {data}
pidfile {_REDIS_PIDFILE}
logfile {_REDIS_LOG}

# Persistence — AOF for durability, RDB for snapshot exports
appendonly yes
appendfsync everysec
auto-aof-rewrite-percentage 100
auto-aof-rewrite-min-size 64mb

# RDB triggers (mirrored to dataset by snapshot_redis.py)
save 900 1
save 300 10
save 60 10000

# Memory cap — bank-playground tier
maxmemory {maxmem}
maxmemory-policy allkeys-lru

daemonize yes
"""
        )
        # 0600 because pw is in there.
        os.chmod(_REDIS_CONF, 0o600)

    def _launch(self) -> None:
        subprocess.run(["redis-server", _REDIS_CONF], check=True)

    def _ping(self, port: int, pw: str) -> bool:
        r = subprocess.run(
            ["redis-cli", "-h", "127.0.0.1", "-p", str(port),
             "-a", pw, "--no-auth-warning", "PING"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and "PONG" in r.stdout

    def snapshot_env(self, cfg: dict) -> dict[str, str]:
        return {
            "DD_REDIS_PORT": str(cfg.get("port", self.default_port)),
            "DD_REDIS_PASSWORD": cfg["password"],
        }


_register(RedisAdapter())
