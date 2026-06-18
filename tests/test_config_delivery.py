"""Tests for the happy-path config delivery.

The wizard stashes each DB's config as a base64-JSON project env var keyed by
the App instance id (DD_CFG_<DOMINO_RUN_ID>), set right after start. The
container reads DD_CFG_<run_id> directly at boot — no Domino API, no file scan,
no app-id matching, no fallbacks.
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
# find_config — happy path
# ---------------------------------------------------------------------------
def _clear(monkeypatch):
    for v in ("DD_CONFIG_JSON", "DOMINO_RUN_ID"):
        monkeypatch.delenv(v, raising=False)
    # Clear any leaked DD_CFG_* vars from the dev shell.
    import os
    for k in [k for k in os.environ if k.startswith("DD_CFG_")]:
        monkeypatch.delenv(k, raising=False)


def test_find_config_from_run_id_env(monkeypatch):
    cfg = {"engine": "postgres", "db_id": "pg-fast"}
    _clear(monkeypatch)
    monkeypatch.setenv("DOMINO_RUN_ID", "inst-123")
    monkeypatch.setenv("DD_CFG_INST-123", _b64(cfg))
    assert lc.find_config() == cfg


def test_find_config_run_id_is_uppercased(monkeypatch):
    # DOMINO_RUN_ID is hex; the var name uses the uppercased form.
    cfg = {"engine": "postgres", "db_id": "pg-up"}
    _clear(monkeypatch)
    monkeypatch.setenv("DOMINO_RUN_ID", "6a3436fab80e4d11")
    monkeypatch.setenv("DD_CFG_6A3436FAB80E4D11", _b64(cfg))
    assert lc.find_config() == cfg


def test_find_config_inline_json_takes_precedence(monkeypatch):
    cfg = {"engine": "postgres", "db_id": "pg-inline"}
    _clear(monkeypatch)
    monkeypatch.setenv("DD_CONFIG_JSON", json.dumps(cfg))
    assert lc.find_config() == cfg


def test_find_config_raises_when_var_missing(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DOMINO_RUN_ID", "inst-xyz")
    # No DD_CFG_INST-XYZ set.
    with pytest.raises(RuntimeError, match="DD_CFG_INST-XYZ is not set"):
        lc.find_config()


def test_find_config_error_lists_present_vars(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DOMINO_RUN_ID", "inst-xyz")
    monkeypatch.setenv("DD_CFG_OTHER", _b64({"x": 1}))
    with pytest.raises(RuntimeError, match="DD_CFG_OTHER"):
        lc.find_config()
