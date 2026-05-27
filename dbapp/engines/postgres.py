"""Postgres engine adapter.

Wraps the existing lifecycle.start_postgres / restore_or_init_postgres /
start_pgbouncer / start_pgweb so nothing about Postgres's runtime behavior
changes — only relocation. The pgweb explorer stays as-is (already works,
already pre-connected with --prefix=admin).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

from . import EngineAdapter, AdminUISpec, ConnectionSnippet, _register
from . import _common


class PostgresAdapter(EngineAdapter):
    name = "postgres"
    docs_label = "PostgreSQL"
    icon = ""
    description = "Relational database with rich SQL and JSON support"
    default_port = 5432
    app_prefix = "pg-"
    env_id_var = "DD_POSTGRES_ENV_ID"

    # ----- lifecycle -----
    def restore_or_init(self, cfg: dict) -> str:
        # Delegate to the existing function for bit-for-bit equivalence.
        from dbapp import lifecycle
        # restore_or_init_postgres returns None; we map to legacy
        # state strings so callers don't special-case Postgres.
        pgdata = _common.data_path(cfg, "pgdata", "/mnt/db/pgdata")
        already_populated = pgdata.exists() and any(pgdata.iterdir())
        lifecycle.restore_or_init_postgres(cfg)
        if already_populated:
            return "noop"
        # Heuristic: if a base.tar.gz existed before init, it was a restore.
        snap = _common.snapshot_path(cfg) / "basebackup" / "base.tar.gz"
        return "restore" if snap.exists() else "fresh"

    def start(self, cfg: dict) -> int | None:
        from dbapp import lifecycle
        lifecycle.start_postgres(cfg)
        pb_port = lifecycle.start_pgbouncer(cfg)
        return pb_port  # may be None → router falls back to cfg["port"]

    def shutdown(self, cfg: dict) -> None:
        # Graceful pg_ctl stop. Best-effort — pg_basebackup teardown via the
        # existing /tmp/dd-final-snapshot.sh is the durable-data path.
        pgdata = cfg.get("pgdata", "/mnt/db/pgdata")
        try:
            subprocess.run(
                ["/usr/lib/postgresql/16/bin/pg_ctl", "-D", pgdata,
                 "-m", "fast", "stop"],
                timeout=20, check=False,
            )
        except Exception as e:
            sys.stderr.write(f"[postgres.shutdown] {e}\n")

    def health_check(self, cfg: dict) -> bool:
        # pgbouncer-or-pg port. The router caches client_port; we recompute
        # to keep this method self-contained.
        port = cfg.get("client_port") or cfg.get("port", self.default_port)
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            return False

    # ----- admin UI -----
    def admin_ui_spec(self, cfg: dict) -> AdminUISpec | None:
        if not Path("/usr/local/bin/pgweb").exists():
            return None
        port = cfg.get("admin_port", 8978)
        pg_port = cfg.get("port", self.default_port)
        user = cfg.get("user", self.default_user)
        pw = cfg["password"]
        url = f"postgres://{user}:{pw}@127.0.0.1:{pg_port}/postgres?sslmode=disable"
        return AdminUISpec(
            argv=[
                "/usr/local/bin/pgweb",
                "--bind", "127.0.0.1",
                "--listen", str(port),
                "--prefix", "admin",
                "--url", url,
                "--skip-open",
                "--lock-session",
            ],
            internal_port=port,
            env={},
            log_path="/var/log/dd/pgweb.log",
            prefix_aware=True,
        )

    # ----- connection snippets -----
    def connection_strings(self, cfg: dict, client_port: int) -> list[ConnectionSnippet]:
        user = cfg.get("user", self.default_user)
        return [
            ConnectionSnippet(
                label="psql",
                snippet=(
                    f'psql "host=127.0.0.1 port={client_port} '
                    f'user={user} dbname=postgres"'
                ),
            ),
            ConnectionSnippet(
                label="DBeaver / DataGrip",
                snippet=(
                    f"New PostgreSQL connection → host=localhost "
                    f"port={client_port} user={user} SSL=off"
                ),
            ),
        ]


_register(PostgresAdapter())
