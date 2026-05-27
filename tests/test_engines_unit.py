"""Unit-style sanity checks for the engine registry.

No network, no Domino, no real DBs — these run anywhere with just Python.
Catches the dumb stuff (a new engine forgetting to set app_prefix, an
adapter raising on import, etc.) before the e2e harness even starts.

Run: python3 tests/test_engines_unit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbapp import engines  # noqa: E402
from dbapp.engines import EngineAdapter, AdminUISpec, ConnectionSnippet  # noqa: E402
from dbapp.engines import _common  # noqa: E402


def main() -> int:
    failures: list[str] = []

    # 1. registry has all four engines, in stable order
    expected = ["postgres", "mongo", "mysql", "redis"]
    got = engines.names()
    if got != expected:
        failures.append(f"registry names: expected {expected}, got {got}")

    # 2. each adapter exposes the required metadata
    seen_ports: dict[int, str] = {}
    seen_prefixes: dict[str, str] = {}
    for a in engines.all_engines():
        for attr in ("name", "docs_label", "description",
                     "default_port", "app_prefix", "env_id_var"):
            if not getattr(a, attr, None):
                failures.append(f"{a.name}: missing {attr}")
        # Ports must be unique across engines.
        prior = seen_ports.get(a.default_port)
        if prior and prior != a.name:
            failures.append(
                f"port collision: {a.name} and {prior} both use "
                f"{a.default_port}"
            )
        seen_ports[a.default_port] = a.name
        # App-name prefixes must be unique.
        prior = seen_prefixes.get(a.app_prefix)
        if prior and prior != a.name:
            failures.append(
                f"prefix collision: {a.name} and {prior} both use "
                f"{a.app_prefix!r}"
            )
        seen_prefixes[a.app_prefix] = a.name

    # 3. snapshot_env returns dict with non-empty values
    for a in engines.all_engines():
        env = a.snapshot_env({
            "db_id": "test", "password": "pw", "port": a.default_port,
            "user": "domino",
        })
        if not env:
            failures.append(f"{a.name}: empty snapshot_env")
        for k, v in env.items():
            if not isinstance(v, str):
                failures.append(f"{a.name}: snapshot_env[{k}] not str: {v!r}")

    # 4. connection_strings returns at least one snippet per engine
    for a in engines.all_engines():
        snips = a.connection_strings({
            "db_id": "test", "password": "pw", "port": a.default_port,
            "user": "domino",
        }, client_port=a.default_port)
        if not snips:
            failures.append(f"{a.name}: empty connection_strings")
        for s in snips:
            if not (s.label and s.snippet):
                failures.append(f"{a.name}: malformed snippet {s}")

    # 5. snapshot_script_name matches snapshotter/*.py shipped in the repo
    snap_root = Path(__file__).resolve().parent.parent / "snapshotter"
    for a in engines.all_engines():
        script = snap_root / a.snapshot_script_name()
        if not script.exists():
            failures.append(f"{a.name}: snapshot script missing at {script}")

    # 6. redact strips obvious passwords
    samples = [
        ("password=hunter2 next", "password=**** next"),
        ('password="hunter2"', 'password=****'),
        ("postgres://u:hunter2@h/d", "postgres://u:****@h/d"),
        ("PGPASSWORD=hunter2", "PGPASSWORD=****"),
        ("DD_MYSQL_PASSWORD=hunter2", "DD_MYSQL_PASSWORD=****"),
    ]
    for raw, want in samples:
        got = _common.redact(raw)
        if got != want:
            failures.append(f"redact({raw!r}): expected {want!r}, got {got!r}")

    if failures:
        print(f"FAIL — {len(failures)} issue(s):")
        for f in failures:
            print(f"  • {f}")
        return 1
    print(f"OK — {len(engines.all_engines())} engines registered cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
