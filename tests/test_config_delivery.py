"""Tests for the robust project-env config-delivery channel.

The wizard stashes each DB's config as a project env var DD_CFG_<app_id>
(base64 JSON); the lifecycle resolves its own app_id from DOMINO_RUN_ID via a
self-contained proxy call and decodes it. This channel survives the run
container's git checkout and needs no domino_api import in the target project —
the failure modes that left boot with "No config file found".
"""
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dbapp.lifecycle as lc  # noqa: E402


def _b64(d):
    return base64.b64encode(json.dumps(d).encode()).decode()


# ---------------------------------------------------------------------------
# _app_from_run_id — match the App by current instance id
# ---------------------------------------------------------------------------
def test_app_from_run_id_matches(monkeypatch):
    apps = [
        {"id": "app-A", "currentVersion": {"currentInstance": {"id": "run-1"}}},
        {"id": "app-B", "currentVersion": {"currentInstance": {"id": "run-2"}}},
    ]
    monkeypatch.setattr(lc, "_fetch_apps_via_api", lambda: apps)
    assert lc._app_from_run_id("run-2")["id"] == "app-B"
    assert lc._app_from_run_id("run-x") is None


# ---------------------------------------------------------------------------
# _config_from_project_env
# ---------------------------------------------------------------------------
def test_config_from_project_env_decodes(monkeypatch):
    cfg = {"engine": "postgres", "db_id": "pg-red", "snapshot_dir": "/mnt/data/db-pg-red"}
    monkeypatch.setattr(lc, "_app_from_run_id", lambda r: {"id": "6a32C9ea"})
    monkeypatch.setenv("DD_CFG_6A32C9EA", _b64(cfg))
    assert lc._config_from_project_env("run-1") == cfg


def test_config_from_project_env_no_app(monkeypatch):
    monkeypatch.setattr(lc, "_app_from_run_id", lambda r: None)
    assert lc._config_from_project_env("run-1") is None


def test_config_from_project_env_var_absent(monkeypatch):
    monkeypatch.setattr(lc, "_app_from_run_id", lambda r: {"id": "app-1"})
    monkeypatch.delenv("DD_CFG_APP-1", raising=False)
    assert lc._config_from_project_env("run-1") is None


def test_config_from_project_env_bad_b64(monkeypatch):
    monkeypatch.setattr(lc, "_app_from_run_id", lambda r: {"id": "app1"})
    monkeypatch.setenv("DD_CFG_APP1", "!!!not-base64!!!")
    assert lc._config_from_project_env("run-1") is None


# ---------------------------------------------------------------------------
# find_config — the project-env channel is consulted before file fallbacks
# ---------------------------------------------------------------------------
def test_find_config_prefers_project_env(monkeypatch, tmp_path):
    cfg = {"engine": "postgres", "db_id": "pg-red"}
    monkeypatch.delenv("DD_CONFIG_JSON", raising=False)
    monkeypatch.delenv("DD_CONFIG", raising=False)
    monkeypatch.delenv("DOMINO_APP_NAME", raising=False)
    monkeypatch.setenv("DOMINO_RUN_ID", "run-1")
    monkeypatch.setattr(lc, "_app_from_run_id", lambda r: {"id": "appX", "name": "pg-red"})
    monkeypatch.setenv("DD_CFG_APPX", _b64(cfg))
    # Even if no config file exists anywhere, the env channel resolves it.
    monkeypatch.setattr(lc, "DBAPPS_DIR", tmp_path / "nope")
    assert lc.find_config() == cfg


def test_find_config_falls_through_when_env_channel_empty(monkeypatch, tmp_path):
    """No env config and no files → the original RuntimeError, unchanged."""
    for v in ("DD_CONFIG_JSON", "DD_CONFIG", "DOMINO_APP_NAME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DOMINO_RUN_ID", "run-1")
    monkeypatch.setattr(lc, "_app_from_run_id", lambda r: None)
    monkeypatch.setenv("DD_ENGINE", "postgres")
    empty = tmp_path / "configs"
    empty.mkdir()
    monkeypatch.setattr(lc, "DBAPPS_DIR", empty)
    # /mnt/code/dbapps is the other hard-coded search dir; point find_config at
    # dirs with no matching json by ensuring neither has any.
    with pytest.raises(RuntimeError, match="No config file found"):
        lc.find_config()


# ---------------------------------------------------------------------------
# _fetch_apps_via_api — self-contained, tolerant of shapes
# ---------------------------------------------------------------------------
def test_fetch_apps_requires_credentials(monkeypatch):
    monkeypatch.delenv("DOMINO_USER_API_KEY", raising=False)
    monkeypatch.delenv("DOMINO_PROJECT_ID", raising=False)
    assert lc._fetch_apps_via_api() == []


def test_fetch_apps_unwraps_and_uses_requests(monkeypatch):
    monkeypatch.setenv("DOMINO_USER_API_KEY", "k")
    monkeypatch.setenv("DOMINO_PROJECT_ID", "P1")
    monkeypatch.setenv("DOMINO_API_PROXY", "http://proxy")

    captured = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"id": "app-1"}]}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers)
        return FakeResp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    apps = lc._fetch_apps_via_api()
    assert apps == [{"id": "app-1"}]
    assert captured["url"] == "http://proxy/v4/modelProducts"
    assert captured["params"] == {"projectId": "P1"}
    assert captured["headers"]["X-Domino-Api-Key"] == "k"


def test_fetch_apps_swallows_errors(monkeypatch):
    monkeypatch.setenv("DOMINO_USER_API_KEY", "k")
    monkeypatch.setenv("DOMINO_PROJECT_ID", "P1")
    import requests

    def boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(requests, "get", boom)
    assert lc._fetch_apps_via_api() == []  # never raises
