"""MySQL 8.0 engine adapter.

Uses the Oracle apt-repo build of MySQL 8.0. No pooler in v1 — MySQL's
thread-per-connection model makes pgbouncer-style multiplexing far less
impactful than for Postgres (which forks per connection).

Explorer: phpMyAdmin (Apache + PHP-FPM), pre-configured with cfg
credentials so the user lands on the SQL editor with no login.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import EngineAdapter, AdminUISpec, ConnectionSnippet, _register
from . import _common

_MYSQL_LOG = "/var/log/dd/mysqld.log"
_MYSQL_ERROR_LOG = "/var/log/dd/mysqld.err"
_MYSQL_INIT_LOG = "/var/log/dd/mysqld-init.err"
_MYSQL_PIDFILE = "/mnt/db/mysql.pid"
_MYSQL_SOCKET = "/mnt/db/sock/mysqld.sock"


class MySQLAdapter(EngineAdapter):
    name = "mysql"
    docs_label = "MySQL"
    icon = ""
    description = "Widely-used relational SQL database"
    default_port = 3306
    app_prefix = "mysql-"
    env_id_var = "DD_MYSQL_ENV_ID"

    # ----- lifecycle -----
    def restore_or_init(self, cfg: dict) -> str:
        data = _common.data_path(cfg, "mysql")
        data.mkdir(parents=True, exist_ok=True)
        snap_root = _common.snapshot_path(cfg)
        snap_root.mkdir(parents=True, exist_ok=True)

        # Already-populated data dir = warm restart.
        if (data / "mysql").exists() or (data / "ibdata1").exists():
            return "noop"

        # mysqld --initialize-insecure requires an EMPTY data dir — strip
        # any lost+found etc. so init doesn't trip on first boot of a
        # fresh mount.
        for entry in data.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                try: entry.unlink()
                except OSError: pass

        dump_gz = snap_root / "snapshots" / "latest" / "dump.sql.gz"
        if dump_gz.exists():
            sys.stderr.write(f"[mysql] init+restore from {dump_gz}\n")
            self._initialize(data)
            return "restore"

        sys.stderr.write("[mysql] initializing fresh data dir\n")
        self._initialize(data)
        return "fresh"

    def start(self, cfg: dict) -> int | None:
        data = _common.data_path(cfg, "mysql")
        port = int(cfg.get("port", self.default_port))
        state = self.restore_or_init(cfg)

        Path("/var/log/dd").mkdir(parents=True, exist_ok=True)
        Path(_MYSQL_SOCKET).parent.mkdir(parents=True, exist_ok=True)

        self._launch(data, port)
        if not _common.wait_for_check(
            lambda: self._ping(port), timeout_s=45,
        ):
            _common.dump_log_tail(_MYSQL_ERROR_LOG, "mysql")
            raise RuntimeError("mysqld failed to become ready in 45s")

        if state == "fresh":
            self._bootstrap_user(port, cfg)
        elif state == "restore":
            self._bootstrap_user(port, cfg)  # creds first, then load dump
            self._restore_dump(port, cfg)
        return None

    def shutdown(self, cfg: dict) -> None:
        port = int(cfg.get("port", self.default_port))
        pw = cfg["password"]
        user = cfg.get("user", self.default_user)
        try:
            subprocess.run(
                ["mysqladmin", "-h", "127.0.0.1", "-P", str(port),
                 "-u", user, f"-p{pw}", "shutdown"],
                timeout=20, check=False,
            )
        except Exception as e:
            sys.stderr.write(f"[mysql.shutdown] {e}\n")

    def health_check(self, cfg: dict) -> bool:
        port = int(cfg.get("port", self.default_port))
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            return False

    # ----- admin UI: phpMyAdmin via Apache + PHP-FPM -----
    def admin_ui_spec(self, cfg: dict) -> AdminUISpec | None:
        if not Path("/etc/dd/apache2.conf").exists():
            return None
        port = int(cfg.get("admin_port", 8978))
        mysql_port = int(cfg.get("port", self.default_port))
        user = cfg.get("user", self.default_user)
        pw = cfg["password"]
        return AdminUISpec(
            # Apache foreground; phpMyAdmin lives at /admin/ via DocumentRoot
            # rewrite configured in /etc/dd/apache2.conf (baked in env).
            argv=[
                "apache2", "-f", "/etc/dd/apache2.conf",
                "-D", "FOREGROUND",
            ],
            internal_port=port,
            env={
                "DD_MYSQL_HOST": "127.0.0.1",
                "DD_MYSQL_PORT": str(mysql_port),
                "DD_MYSQL_USER": user,
                "DD_MYSQL_PASSWORD": pw,
                "DD_ADMIN_PORT": str(port),
            },
            log_path="/var/log/dd/apache2.log",
            prefix_aware=True,
        )

    # ----- connection snippets -----
    def connection_strings(self, cfg: dict, client_port: int) -> list[ConnectionSnippet]:
        user = cfg.get("user", self.default_user)
        return [
            ConnectionSnippet(
                label="mysql CLI",
                snippet=(
                    f"mysql -h 127.0.0.1 -P {client_port} "
                    f"-u {user} -p"
                ),
                note="enter the admin password when prompted",
            ),
            ConnectionSnippet(
                label="DBeaver / DataGrip",
                snippet=(
                    f"New MySQL connection → host=localhost "
                    f"port={client_port} user={user} useSSL=false"
                ),
            ),
        ]

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _initialize(self, data: Path) -> None:
        # --initialize-insecure: root@localhost gets no password; we set
        # the cfg password ourselves in _bootstrap_user. The "insecure"
        # name is misleading — bind_address is 127.0.0.1 by default and
        # we add a real password before opening the port to /wire.
        # --log-error: the apt-packaged defaults file points at
        # /var/log/mysql/error.log which is root-only; we run as ubuntu.
        Path(_MYSQL_INIT_LOG).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["mysqld", "--initialize-insecure",
             f"--datadir={data}",
             f"--log-error={_MYSQL_INIT_LOG}"],
            check=True,
        )

    def _launch(self, data: Path, port: int) -> None:
        log_handle = open(_MYSQL_ERROR_LOG, "a")
        # mysqld in our env has no init script; we fork manually so it
        # survives this Python process exit.
        subprocess.Popen(
            ["mysqld",
             f"--datadir={data}",
             f"--port={port}",
             "--bind-address=127.0.0.1",
             f"--socket={_MYSQL_SOCKET}",
             f"--pid-file={_MYSQL_PIDFILE}",
             f"--log-error={_MYSQL_ERROR_LOG}",
             # X protocol's default unix-socket path (/var/run/mysqld/)
             # isn't writable as ubuntu; point it next to the classic
             # socket. We don't use mysqlx, but the warning is noisy.
             f"--mysqlx-socket={Path(_MYSQL_SOCKET).parent / 'mysqlx.sock'}",
             # Modest memory cap — banks-playground tier, not OLTP.
             "--innodb-buffer-pool-size=512M",
             # Skip name resolution (faster connects, no DNS dep).
             "--skip-name-resolve"],
            stdout=log_handle, stderr=log_handle,
            start_new_session=True, close_fds=True,
        )

    def _ping(self, port: int) -> bool:
        # Ping over the unix socket as root — root@localhost from
        # --initialize-insecure has no password but is socket-only;
        # TCP ping pre-bootstrap fails auth (no ubuntu@% user yet).
        r = subprocess.run(
            ["mysqladmin", f"--socket={_MYSQL_SOCKET}", "-u", "root", "ping"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and "alive" in r.stdout

    def _bootstrap_user(self, port: int, cfg: dict) -> None:
        user = cfg.get("user", self.default_user)
        pw = cfg["password"]
        # We talk to mysqld over the socket as root (passwordless from
        # --initialize-insecure). One transaction, idempotent.
        # CREATE USER + GRANT done with IF NOT EXISTS to survive
        # restore-and-rebootstrap (e.g. dump.sql may have already
        # created the user).
        script = (
            f"CREATE USER IF NOT EXISTS '{user}'@'127.0.0.1' "
            f"IDENTIFIED WITH mysql_native_password BY '{pw}';\n"
            f"CREATE USER IF NOT EXISTS '{user}'@'%' "
            f"IDENTIFIED WITH mysql_native_password BY '{pw}';\n"
            f"GRANT ALL PRIVILEGES ON *.* TO '{user}'@'127.0.0.1' WITH GRANT OPTION;\n"
            f"GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%' WITH GRANT OPTION;\n"
            "FLUSH PRIVILEGES;\n"
        )
        r = subprocess.run(
            ["mysql", f"--socket={_MYSQL_SOCKET}", "-u", "root"],
            input=script, text=True, capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"mysql bootstrap failed: {_common.redact(r.stderr)[:500]}"
            )

    def _restore_dump(self, port: int, cfg: dict) -> None:
        snap_root = _common.snapshot_path(cfg)
        dump_gz = snap_root / "snapshots" / "latest" / "dump.sql.gz"
        sys.stderr.write(f"[mysql] restoring from {dump_gz}\n")
        user = cfg.get("user", self.default_user)
        pw = cfg["password"]
        # gunzip → mysql, no temp file
        gz = subprocess.Popen(
            ["gunzip", "-c", str(dump_gz)],
            stdout=subprocess.PIPE,
        )
        sql = subprocess.Popen(
            ["mysql", "-h", "127.0.0.1", "-P", str(cfg.get("port", self.default_port)),
             "-u", user, f"-p{pw}"],
            stdin=gz.stdout,
        )
        if gz.stdout is not None:
            gz.stdout.close()
        rc = sql.wait()
        gz.wait()
        if rc != 0:
            raise RuntimeError(f"mysql restore failed: rc={rc}")

    def snapshot_env(self, cfg: dict) -> dict[str, str]:
        return {
            "DD_MYSQL_PORT": str(cfg.get("port", self.default_port)),
            "DD_MYSQL_USER": cfg.get("user", self.default_user),
            "DD_MYSQL_PASSWORD": cfg["password"],
        }


_register(MySQLAdapter())
