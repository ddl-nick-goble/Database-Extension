"""Shared helpers for engine adapters — kept tiny on purpose."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------
def snapshot_path(cfg: dict) -> Path:
    """Where this DB's snapshots live. Same shape for every engine — a
    subdir of the project's default dataset keyed by db_id."""
    explicit = cfg.get("snapshot_dir") or os.environ.get("DD_SNAPSHOT_DIR")
    if explicit:
        return Path(explicit)
    base = os.environ.get("DOMINO_DATASETS_DIR", "/mnt/data")
    project = os.environ.get("DOMINO_PROJECT_NAME", "default")
    return Path(base) / project / f"db-{cfg['db_id']}"


def data_path(cfg: dict, engine: str, default: str | None = None) -> Path:
    """Per-engine on-disk data dir. cfg["data_dir"] wins; otherwise
    /mnt/db/<engine> by default. Each engine passes its own default so we
    don't leak engine names into the base class."""
    return Path(cfg.get("data_dir", default or f"/mnt/db/{engine}"))


# --------------------------------------------------------------------------
# Process-launch + wait-for-ready
# --------------------------------------------------------------------------
def wait_for_port(port: int, timeout_s: int = 30, host: str = "127.0.0.1") -> bool:
    """Return True when something is listening on host:port (or timeout)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=0.5)
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def wait_for_check(check, timeout_s: int = 30, interval_s: float = 1.0) -> bool:
    """Poll a callable that returns truthy on success."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if check():
                return True
        except Exception:
            pass
        time.sleep(interval_s)
    return False


def dump_log_tail(path: str, label: str, max_bytes: int = 3000) -> None:
    """Dump the tail of a log file to stderr for failure debugging.

    Passes the output through `redact()` so passwords / connection
    strings don't end up in container logs or Domino's run viewer.
    """
    try:
        with open(path) as f:
            content = f.read()
        sys.stderr.write(f"[{label}] log tail ({path}):\n")
        sys.stderr.write(redact(content[-max_bytes:]))
        sys.stderr.write("\n")
    except OSError as e:
        sys.stderr.write(f"[{label}] could not read {path}: {e}\n")


# --------------------------------------------------------------------------
# Log redaction — strip passwords from anything we dump to logs
# --------------------------------------------------------------------------
_PASSWORD_PATTERNS = [
    # postgres://user:pw@host
    (re.compile(r"://([^:@/]+):([^@/\s]+)@"), r"://\1:****@"),
    # password='...', password="...", password=word
    (re.compile(r"(password\s*[:=]\s*)('[^']*'|\"[^\"]*\"|\S+)", re.IGNORECASE),
     r"\1****"),
    # PGPASSWORD=..., DD_*_PASSWORD=...
    (re.compile(r"(PGPASSWORD|DD_[A-Z]+_PASSWORD)=\S+"),
     r"\1=****"),
]


def redact(text: str) -> str:
    """Best-effort password scrubbing for log output. NOT a security
    boundary — anyone with read access to a real secret can still see it
    via /proc/<pid>/environ. The point is to avoid splatting it into
    /var/log/dd/snapshot.log."""
    if not text:
        return text
    for pattern, repl in _PASSWORD_PATTERNS:
        text = pattern.sub(repl, text)
    return text
