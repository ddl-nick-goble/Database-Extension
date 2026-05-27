"""Engine adapter registry — one source of truth for each DB engine.

Every Domino Databases engine (postgres / mongo / mysql / redis) is plugged
in through an EngineAdapter subclass. lifecycle.boot(), the Flask router,
and the wizard all read from the same registry — adding a new engine is a
single subclass, not five touch-points.

Per-engine modules: dbapp/engines/{postgres,mongo,mysql,redis}.py.
The registry below is populated by import side-effects so consumers can
just call `engines.get("postgres")` without worrying about plugin loading.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

__all__ = [
    "EngineAdapter",
    "AdminUISpec",
    "ConnectionSnippet",
    "get",
    "all_engines",
    "names",
]


# --------------------------------------------------------------------------
# Data classes used across all engines
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AdminUISpec:
    """How the router should expose this engine's admin UI under /admin/.

    The router launches the UI process once (during boot, via lifecycle) and
    reverse-proxies /admin/* to `internal_port`. URL rewriting (e.g.
    pgweb's `--prefix=admin` flag) is the responsibility of the engine
    adapter — the spec just records what got chosen.
    """
    # Argv for the admin process. lifecycle launches via subprocess.Popen.
    argv: list[str]
    # Internal port the admin UI listens on. Router proxies /admin/ → this.
    internal_port: int
    # Optional env vars to inject (e.g. ME_CONFIG_MONGODB_URL).
    env: dict[str, str]
    # Optional log file (relative or absolute). Default: /var/log/dd/admin.log
    log_path: str = "/var/log/dd/admin.log"
    # If True, the admin UI's HTML already uses /admin/ prefixed URLs and the
    # router can do a straight pass-through. If False, the router needs to
    # rewrite Location: headers and HTML; we currently only support True.
    prefix_aware: bool = True


@dataclass(frozen=True)
class ConnectionSnippet:
    """One row on the status page's 'Connect from your laptop' section."""
    label: str       # e.g. "psql", "DBeaver", "mongosh"
    snippet: str     # shell command or connection-string template
    note: str = ""   # optional one-line caveat


class EngineAdapter:
    """Base class — subclass once per engine, instantiate once.

    Subclasses set the class-level metadata and implement the lifecycle
    hooks. Keep methods small; share helpers via dbapp/engines/_common.py.
    """

    name: str                  # "postgres" | "mongo" | "mysql" | "redis"
    docs_label: str            # "Postgres", "MongoDB", "MySQL", "Redis"
    icon: str = ""             # optional short glyph for the wizard UI
    description: str           # short marketing line on the engine card
    default_port: int          # the canonical client-facing port
    app_prefix: str            # "pg-" | "mongo-" | "mysql-" | "redis-"
    default_user: str = "domino"
    env_id_var: str            # e.g. "DD_POSTGRES_ENV_ID" — wizard reads this

    # ------- lifecycle hooks (subclass overrides) -------
    def restore_or_init(self, cfg: dict) -> str:
        """Populate the data dir from a snapshot if one exists, else init
        a fresh cluster. Returns 'fresh' | 'restore' | 'noop'. Engines that
        do post-start setup (e.g. Mongo's user creation) branch on this."""
        raise NotImplementedError

    def start(self, cfg: dict) -> int | None:
        """Start the engine + any pooler. Returns the client-facing port
        the router should send /wire bytes to (e.g. pgbouncer's 6432, or
        the engine's own port). None falls back to cfg['port']."""
        raise NotImplementedError

    def shutdown(self, cfg: dict) -> None:
        """Gracefully stop the engine on SIGTERM. Best-effort — the bash
        teardown trap kills the process anyway if this hangs."""
        raise NotImplementedError

    def health_check(self, cfg: dict) -> bool:
        """Used by /healthz. Should be cheap (~1 RT) — Domino polls often."""
        raise NotImplementedError

    def admin_ui_spec(self, cfg: dict) -> AdminUISpec | None:
        """Describe the OSS explorer UI the router should mount at /admin/.
        Returning None disables /admin/ for this engine."""
        return None

    def connection_strings(self, cfg: dict, client_port: int) -> list[ConnectionSnippet]:
        """Lines rendered on the status page's 'connect from laptop' card."""
        return []

    # ------- snapshotter integration -------
    def snapshot_script_name(self) -> str:
        """Basename of the snapshotter script under snapshotter/."""
        return f"snapshot_{self.name}.py"

    def snapshot_env(self, cfg: dict) -> dict[str, str]:
        """Env vars the snapshotter script reads. Default exports the
        common ones; engines with extra config (e.g. socket path) override."""
        return {
            f"DD_{self.name.upper()}_PORT": str(cfg.get("port", self.default_port)),
            f"DD_{self.name.upper()}_USER": cfg.get("user", self.default_user),
            f"DD_{self.name.upper()}_PASSWORD": cfg["password"],
        }


# --------------------------------------------------------------------------
# Registry — populated by import side-effects of each engine module
# --------------------------------------------------------------------------
_REGISTRY: dict[str, EngineAdapter] = {}


def _register(adapter: EngineAdapter) -> None:
    _REGISTRY[adapter.name] = adapter


def get(name: str) -> EngineAdapter:
    if name not in _REGISTRY:
        raise KeyError(
            f"unknown engine {name!r} — known: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]


def all_engines() -> list[EngineAdapter]:
    """Stable ordering — the wizard renders cards in this order."""
    order = ["postgres", "mongo", "mysql", "redis"]
    return [_REGISTRY[n] for n in order if n in _REGISTRY]


def names() -> list[str]:
    return [a.name for a in all_engines()]


# Side-effect imports — each module registers itself.
# Wrapped in try/except so an env image missing a file (e.g. partial
# rebuild) doesn't break the other engines.
def _load_all() -> None:
    for mod in ("postgres", "mongo", "mysql", "redis"):
        try:
            __import__(f"dbapp.engines.{mod}", fromlist=["_register"])
        except Exception as e:
            sys.stderr.write(f"[engines] WARN: failed to load {mod}: {e}\n")


_load_all()
