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
_REDIS_CONF = "/mnt/db/redis.conf"
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
        # lifecycle.boot() already ran restore_or_init() before start(), same
        # as the Postgres path — don't re-run it here. restore_or_init has
        # populated dump.rdb (or not) on disk; redis-server loads it on launch.

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
        maxmem = self._compute_maxmemory(cfg)
        io_threads = self._io_threads(cfg)
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

# --- Big pipes: throughput tuning ---------------------------------------
# Multi-threaded network I/O (command execution stays single-threaded, but
# socket read/write/parse is parallelized — the real win over a tunnel).
io-threads {io_threads}
io-threads-do-reads yes
# Accept backlog: don't drop bursts of new tunnel connections.
tcp-backlog 511
# Detect half-open peers (the WS tunnel can leave sockets dangling).
tcp-keepalive 300
# Don't let a slow/large reply to a normal client get truncated. 0 0 0 =
# never disconnect a normal client for output buffer size (pub/sub +
# replica keep bounded limits below).
client-output-buffer-limit normal 0 0 0
client-output-buffer-limit pubsub 32mb 8mb 60

# --- Data safety: behave like a database, not a cache -------------------
# Sized to the hardware tier (see _compute_maxmemory), not a flat 1 GB.
maxmemory {maxmem}
# noeviction: when full, REJECT writes with an error instead of silently
# dropping keys. A "database app" must never lose data the user stored.
maxmemory-policy noeviction

daemonize yes
"""
        )
        # 0600 because pw is in there.
        os.chmod(_REDIS_CONF, 0o600)

    def _compute_maxmemory(self, cfg: dict) -> str:
        """maxmemory for this DB.

        Explicit cfg["redis_maxmemory"] wins (operator override). Otherwise
        size it to a fraction of the container's memory limit so a Redis on
        a big hardware tier actually uses the box. We deliberately leave
        generous headroom (default 60%) because:
          - BGSAVE forks the process; copy-on-write can transiently need up
            to another copy of the dataset under heavy writes, and
          - redis-commander (Node) + the Flask router + snapshotter share
            this container.
        Falls back to 1gb if the limit can't be detected.
        """
        override = cfg.get("redis_maxmemory")
        if override:
            return str(override)
        total = _common.container_memory_bytes()
        if not total:
            return "1gb"
        frac = float(cfg.get("redis_maxmemory_fraction", 0.60))
        budget = int(total * frac)
        # Floor so a tiny tier still gets a usable amount.
        budget = max(budget, 256 * 1024 * 1024)
        return _common.human_bytes(budget)

    def _io_threads(self, cfg: dict) -> int:
        """Number of Redis I/O threads. Default: scale with cores, capped at
        4 (Redis docs warn diminishing returns past ~4 on most workloads)."""
        override = cfg.get("redis_io_threads")
        if override:
            return max(1, int(override))
        cores = os.cpu_count() or 1
        return max(1, min(4, cores))

    def _launch(self) -> None:
        # check=False so we can surface the config/startup error ourselves
        # instead of an opaque CalledProcessError — mirrors start_postgres's
        # log-dump-on-failure behavior.
        proc = subprocess.run(
            ["redis-server", _REDIS_CONF],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            sys.stderr.write(
                f"[redis] redis-server exited rc={proc.returncode} on launch.\n"
            )
            if proc.stderr:
                sys.stderr.write(_common.redact(proc.stderr[-2000:]) + "\n")
            _common.dump_log_tail(_REDIS_LOG, "redis")
            raise RuntimeError(
                f"redis-server failed to launch (rc={proc.returncode})"
            )

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
