"""Tests for config delivery.

The wizard stashes each DB's config as a base64-JSON project env var keyed by
the App's stable id (DD_CFG_<app_id>), set at create time. The container
resolves its own app_id from DOMINO_RUN_ID via the Domino API (run/instance ids
aren't stable or known ahead of time) and reads that var.
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


def _clear(monkeypatch):
    import os
    for v in ("DD_CONFIG_JSON", "DOMINO_RUN_ID"):
        monkeypatch.delenv(v, raising=False)
    for k in [k for k in os.environ if k.startswith("DD_CFG_")]:
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# _decode_cfg_env
# ---------------------------------------------------------------------------
def test_decode_cfg_env_ok(monkeypatch):
    cfg = {"engine": "postgres", "db_id": "pg-x"}
    monkeypatch.setenv("DD_CFG_ABC", _b64(cfg))
    assert lc._decode_cfg_env("DD_CFG_ABC") == cfg


def test_decode_cfg_env_absent(monkeypatch):
    monkeypatch.delenv("DD_CFG_NOPE", raising=False)
    assert lc._decode_cfg_env("DD_CFG_NOPE") is None


def test_decode_cfg_env_bad_base64(monkeypatch):
    monkeypatch.setenv("DD_CFG_BAD", "!!!not-base64!!!")
    assert lc._decode_cfg_env("DD_CFG_BAD") is None


# ---------------------------------------------------------------------------
# app_id resolution (the correct /v4/modelProducts field is latestAppInstanceId)
# ---------------------------------------------------------------------------
def test_app_id_from_apps_latest_instance():
    apps = [
        {"id": "app-A", "latestAppInstanceId": "run-1"},
        {"id": "app-B", "latestAppInstanceId": "run-2"},
    ]
    assert lc._app_id_from_apps(apps, "run-2") == "app-B"
    assert lc._app_id_from_apps(apps, "run-x") == ""


def test_app_id_from_apps_fallback_fields():
    apps = [{"id": "app-B", "runningInstanceId": "run-9"}]
    assert lc._app_id_from_apps(apps, "run-9") == "app-B"


def test_resolve_app_id_no_retry_when_found(monkeypatch):
    monkeypatch.setattr(lc, "_fetch_apps_via_api",
                        lambda: [{"id": "app-Q", "latestAppInstanceId": "run-1"}])
    assert lc._resolve_app_id("run-1", timeout_s=0) == "app-Q"


def test_resolve_app_id_gives_up_after_timeout(monkeypatch):
    monkeypatch.setattr(lc, "_fetch_apps_via_api", lambda: [])
    assert lc._resolve_app_id("run-1", timeout_s=0) == ""


# ---------------------------------------------------------------------------
# _fetch_apps_via_api — proxy first, public-host fallback
# ---------------------------------------------------------------------------
def test_fetch_apps_requires_credentials(monkeypatch):
    monkeypatch.delenv("DOMINO_USER_API_KEY", raising=False)
    monkeypatch.delenv("DOMINO_PROJECT_ID", raising=False)
    assert lc._fetch_apps_via_api() == []


def test_fetch_apps_falls_back_to_public_host(monkeypatch):
    monkeypatch.setenv("DOMINO_USER_API_KEY", "k")
    monkeypatch.setenv("DOMINO_PROJECT_ID", "P1")
    monkeypatch.setenv("DOMINO_API_PROXY", "http://localhost:8899")
    monkeypatch.delenv("DOMINO_API_HOST", raising=False)
    monkeypatch.setenv("DOMINO_PUBLIC_HOST", "https://nucleus.example.com")

    class FakeResp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    seen = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append(url)
        if url.startswith("http://localhost:8899"):
            raise ConnectionError("refused")
        return FakeResp({"data": [{"id": "appZ", "latestAppInstanceId": "run-9"}]})

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    apps = lc._fetch_apps_via_api()
    assert apps and apps[0]["id"] == "appZ"
    assert any("localhost:8899" in u for u in seen)
    assert any("nucleus.example.com" in u for u in seen)


# ---------------------------------------------------------------------------
# find_config
# ---------------------------------------------------------------------------
def test_find_config_resolves_app_id_then_reads_var(monkeypatch):
    cfg = {"engine": "postgres", "db_id": "pg-app"}
    _clear(monkeypatch)
    monkeypatch.setenv("DOMINO_RUN_ID", "run-1")
    monkeypatch.setattr(lc, "_resolve_app_id", lambda r, **k: "appX")
    monkeypatch.setenv("DD_CFG_APPX", _b64(cfg))
    assert lc.find_config() == cfg


def test_find_config_inline_json_first(monkeypatch):
    cfg = {"engine": "postgres", "db_id": "pg-inline"}
    _clear(monkeypatch)
    monkeypatch.setenv("DD_CONFIG_JSON", json.dumps(cfg))
    assert lc.find_config() == cfg


def test_find_config_raises_when_app_unresolved(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DOMINO_RUN_ID", "run-1")
    monkeypatch.setattr(lc, "_resolve_app_id", lambda r, **k: "")
    monkeypatch.setattr(lc, "_fetch_apps_via_api", lambda: [])
    with pytest.raises(RuntimeError, match="could not resolve a config"):
        lc.find_config()


def test_find_config_raises_when_var_missing(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DOMINO_RUN_ID", "run-1")
    monkeypatch.setattr(lc, "_resolve_app_id", lambda r, **k: "appX")
    monkeypatch.setattr(lc, "_fetch_apps_via_api", lambda: [])
    # app resolves but DD_CFG_APPX not set
    with pytest.raises(RuntimeError, match="could not resolve a config"):
        lc.find_config()
