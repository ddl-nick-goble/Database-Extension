"""Unit-style coverage for the wizard's env resolution.

This is the test that *should* have shipped with the auto-resolve change —
it exercises both resolution paths (env-var override and name-based
lookup) plus the "missing" case the UI surfaces as an error.

No real Domino API call — we monkey-patch domino_api.list_environments
so the assertions are deterministic. Run:

    python3 tests/test_env_resolution.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import domino_api as dapi  # noqa: E402

# Set minimum env so app.py imports cleanly. The import has to happen
# AFTER we set these (app.py reads $DOMINO_PROJECT_ID at import time).
os.environ.setdefault("DOMINO_PROJECT_ID", "test")
os.environ.setdefault("DOMINO_USER_API_KEY", "x")
os.environ.setdefault("DOMINO_PROJECT_NAME", "test")
# Clear any leftover DD_*_ENV_ID from the host shell so the scenarios
# below control resolution unambiguously.
for var in ("DD_POSTGRES_ENV_ID", "DD_MONGO_ENV_ID",
            "DD_MYSQL_ENV_ID", "DD_REDIS_ENV_ID"):
    os.environ.pop(var, None)

import app as app_module  # noqa: E402


FAILURES: list[str] = []


def assert_eq(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}: expected {want!r}, got {got!r}")


def fake_env_list(envs: list[tuple[str, str]]):
    """Return a stubbed list_environments() that yields {id, name} dicts."""
    def _stub():
        return [{"id": eid, "name": name} for eid, name in envs]
    return _stub


def with_fake_envs(envs, **env_vars):
    """Context manager: monkey-patch list_environments + os.environ vars."""
    class _Ctx:
        def __enter__(self):
            self._orig = dapi.list_environments
            self._orig_env = {k: os.environ.get(k) for k in env_vars}
            dapi.list_environments = fake_env_list(envs)
            for k, v in env_vars.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        def __exit__(self, *a):
            dapi.list_environments = self._orig
            for k, v in self._orig_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return _Ctx()


def fetch_engines():
    client = app_module.app.test_client()
    r = client.get("/api/config").get_json()
    return {e["name"]: e for e in r["engines"]}


# --------------------------------------------------------------------------
# Scenario 1: all four envs exist by canonical name → all resolve by name
# --------------------------------------------------------------------------
def scenario_byname_only():
    catalog = [
        ("PG_ID",    "dd-postgres-app"),
        ("MONGO_ID", "dd-mongo-app"),
        ("MYSQL_ID", "dd-mysql-app"),
        ("REDIS_ID", "dd-redis-app"),
        ("OTHER",    "some-unrelated-env"),
    ]
    with with_fake_envs(catalog):
        e = fetch_engines()
        assert_eq("byname.postgres.envId",       e["postgres"]["envId"],       "PG_ID")
        assert_eq("byname.postgres.envIdSource", e["postgres"]["envIdSource"], "byname")
        assert_eq("byname.mongo.envId",          e["mongo"]["envId"],          "MONGO_ID")
        assert_eq("byname.mysql.envId",          e["mysql"]["envId"],          "MYSQL_ID")
        assert_eq("byname.redis.envId",          e["redis"]["envId"],          "REDIS_ID")
        assert_eq("byname.redis.envIdSource",    e["redis"]["envIdSource"],    "byname")


# --------------------------------------------------------------------------
# Scenario 2: explicit env vars override the name-based fallback
# --------------------------------------------------------------------------
def scenario_envvar_overrides_byname():
    catalog = [
        ("PG_BYNAME", "dd-postgres-app"),   # name matches…
    ]
    with with_fake_envs(catalog, DD_POSTGRES_ENV_ID="PG_OVERRIDE"):
        e = fetch_engines()
        assert_eq("override.postgres.envId",       e["postgres"]["envId"],       "PG_OVERRIDE")
        assert_eq("override.postgres.envIdSource", e["postgres"]["envIdSource"], "envvar")


# --------------------------------------------------------------------------
# Scenario 3: no env, no env var → "missing" so the UI can surface an error
# --------------------------------------------------------------------------
def scenario_missing_when_neither():
    with with_fake_envs([("OTHER", "irrelevant")]):
        e = fetch_engines()
        for engine in ("postgres", "mongo", "mysql", "redis"):
            assert_eq(f"missing.{engine}.envId",       e[engine]["envId"],       "")
            assert_eq(f"missing.{engine}.envIdSource", e[engine]["envIdSource"], "missing")
            assert_eq(f"missing.{engine}.expectedEnvName",
                      e[engine]["expectedEnvName"], f"dd-{engine}-app")


# --------------------------------------------------------------------------
# Scenario 4: mixed — postgres by name, redis by env var, others missing
# --------------------------------------------------------------------------
def scenario_mixed():
    catalog = [("PG_X", "dd-postgres-app")]
    with with_fake_envs(catalog, DD_REDIS_ENV_ID="REDIS_X"):
        e = fetch_engines()
        assert_eq("mixed.postgres.source", e["postgres"]["envIdSource"], "byname")
        assert_eq("mixed.postgres.envId",  e["postgres"]["envId"],       "PG_X")
        assert_eq("mixed.mongo.source",    e["mongo"]["envIdSource"],    "missing")
        assert_eq("mixed.mysql.source",    e["mysql"]["envIdSource"],    "missing")
        assert_eq("mixed.redis.source",    e["redis"]["envIdSource"],    "envvar")
        assert_eq("mixed.redis.envId",     e["redis"]["envId"],          "REDIS_X")


# --------------------------------------------------------------------------
# Scenario 5: Domino API failing must not crash the wizard endpoint —
# all engines should fall back to "missing" cleanly.
# --------------------------------------------------------------------------
def scenario_api_failure_is_graceful():
    def boom():
        raise RuntimeError("simulated API failure")
    orig = dapi.list_environments
    dapi.list_environments = boom
    try:
        e = fetch_engines()
        for engine in ("postgres", "mongo", "mysql", "redis"):
            assert_eq(f"apifail.{engine}.envId", e[engine]["envId"], "")
            assert_eq(f"apifail.{engine}.envIdSource",
                      e[engine]["envIdSource"], "missing")
    finally:
        dapi.list_environments = orig


def main() -> int:
    scenario_byname_only()
    scenario_envvar_overrides_byname()
    scenario_missing_when_neither()
    scenario_mixed()
    scenario_api_failure_is_graceful()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} assertion(s) failed:")
        for f in FAILURES:
            print(f"  • {f}")
        return 1
    print("OK — env resolution: byname, envvar-override, missing, mixed, "
          "and API-failure paths all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
