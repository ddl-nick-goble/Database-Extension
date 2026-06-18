"""Tests for the workspace connection manifest + .pgpass writer.

dd-workspace-setup.py builds ~/.dd/connections.json and ~/.pgpass so a script
can connect to every DB in the project at once. The file is hyphen-named and
probes the network at import unless DOMINO_PUBLIC_HOST is set, so we set that
and load it via importlib.
"""
import base64
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

# Skip the import-time cliSiteConfig network probe.
os.environ.setdefault("DOMINO_PUBLIC_HOST", "https://x.example.com")

_PATH = Path(__file__).resolve().parent.parent / "client" / "dd-workspace-setup.py"
_spec = importlib.util.spec_from_file_location("dd_workspace_setup", _PATH)
ws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws)


def _app(engine_prefix, app_id="app-1", name=None):
    eng = {"pg-": "postgres", "mongo-": "mongo", "mysql-": "mysql", "redis-": "redis"}[engine_prefix]
    return {"id": app_id, "name": name or f"{engine_prefix}db", "_engine": eng}


# ---------------------------------------------------------------------------
# _conn_details — per-engine URIs
# ---------------------------------------------------------------------------
def test_conn_details_postgres_with_password():
    rec = ws._conn_details(_app("pg-"), 5432, {"user": "domino", "password": "Passw0rd!"})
    assert rec["engine"] == "postgres"
    assert rec["host"] == "127.0.0.1" and rec["port"] == 5432
    assert rec["dbname"] == "postgres"
    assert rec["uri"] == "postgresql://domino:Passw0rd%21@127.0.0.1:5432/postgres"
    assert rec["password"] == "Passw0rd!"


def test_conn_details_redis_password_userinfo():
    rec = ws._conn_details(_app("redis-"), 6379, {"password": "sekret"})
    assert rec["uri"] == "redis://:sekret@127.0.0.1:6379/0"


def test_conn_details_mongo_and_mysql():
    m = ws._conn_details(_app("mongo-"), 27017, {"user": "domino", "password": "p"})
    assert m["uri"] == "mongodb://domino:p@127.0.0.1:27017/?authSource=admin"
    y = ws._conn_details(_app("mysql-"), 3306, {"user": "domino", "password": "p"})
    assert y["uri"] == "mysql://domino:p@127.0.0.1:3306/"


def test_conn_details_no_password_omits_creds():
    rec = ws._conn_details(_app("pg-"), 5432, {})  # no DD_CFG → no password
    assert "password" not in rec
    assert rec["uri"] == "postgresql://domino@127.0.0.1:5432/postgres"


def test_conn_details_url_encodes_special_chars():
    rec = ws._conn_details(_app("pg-"), 5432, {"user": "a/b", "password": "p@ss:w/rd"})
    # '@', ':', '/' in the password must be percent-encoded so the URI parses.
    assert "p%40ss%3Aw%2Frd" in rec["uri"]
    assert "a%2Fb" in rec["uri"]


# ---------------------------------------------------------------------------
# _decode_app_cfg
# ---------------------------------------------------------------------------
def test_decode_app_cfg_reads_project_env(monkeypatch):
    cfg = {"engine": "postgres", "user": "domino", "password": "pw"}
    monkeypatch.setenv("DD_CFG_APP-1", base64.b64encode(json.dumps(cfg).encode()).decode())
    assert ws._decode_app_cfg({"id": "app-1"}) == cfg


def test_decode_app_cfg_missing(monkeypatch):
    monkeypatch.delenv("DD_CFG_APP-2", raising=False)
    assert ws._decode_app_cfg({"id": "app-2"}) == {}
    assert ws._decode_app_cfg({}) == {}


# ---------------------------------------------------------------------------
# _pgpass_escape
# ---------------------------------------------------------------------------
def test_pgpass_escape():
    assert ws._pgpass_escape("a:b\\c") == "a\\:b\\\\c"


# ---------------------------------------------------------------------------
# manifest + .pgpass file writers
# ---------------------------------------------------------------------------
def test_write_connections_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    entries = {"pg-db": ws._conn_details(_app("pg-"), 5432, {"user": "domino", "password": "pw"})}
    out = ws._write_connections_manifest(entries)
    assert out == tmp_path / ".dd" / "connections.json"
    assert json.loads(out.read_text())["pg-db"]["port"] == 5432
    # 0600
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_write_pgpass_writes_and_preserves(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Pre-existing unrelated entry must survive; a stale line for our port is replaced.
    pgpass = tmp_path / ".pgpass"
    pgpass.write_text("other-host:5432:*:bob:secret\n127.0.0.1:5432:*:old:stale\n")

    entries = {
        "pg-a": ws._conn_details(_app("pg-", "app-a", "pg-a"), 5432, {"user": "domino", "password": "pw1"}),
        "pg-b": ws._conn_details(_app("pg-", "app-b", "pg-b"), 5433, {"user": "domino", "password": "pw2"}),
        "redis-c": ws._conn_details(_app("redis-", "app-c", "redis-c"), 6379, {"password": "x"}),
    }
    out = ws._write_pgpass(entries)
    lines = out.read_text().splitlines()
    assert "other-host:5432:*:bob:secret" in lines          # unrelated kept
    assert "127.0.0.1:5432:*:old:stale" not in lines         # stale replaced
    assert "127.0.0.1:5432:*:domino:pw1" in lines
    assert "127.0.0.1:5433:*:domino:pw2" in lines
    assert not any(":6379:" in ln for ln in lines)           # redis not in .pgpass
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_write_pgpass_none_when_no_pg(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    entries = {"redis-c": ws._conn_details(_app("redis-", "app-c", "redis-c"), 6379, {"password": "x"})}
    assert ws._write_pgpass(entries) is None
