"""Tests for per-DB backup-dataset creation in the wizard create flow.

Covers the fix for cross-project DB provisioning: each DB now creates a
dedicated Domino dataset in the TARGET project and bakes its mount path into
the config (snapshot_dir) so backups land in a real, mounted dataset rather
than an assumed /mnt/data/<project>/... path.
"""
import base64
import json
import os
import sys
from pathlib import Path

import pytest

# Ensure the repo root is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import domino_api as dapi  # noqa: E402


# ---------------------------------------------------------------------------
# _dataset_name — Domino-safe name derivation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("full_name,expected", [
    ("pg-market", "db-pg-market"),
    ("pg-Market Data", "db-pg-market-data"),
    ("redis-foo__bar!", "db-redis-foo-bar"),
    ("mongo-UPPER", "db-mongo-upper"),
    ("---weird---", "db-weird"),
])
def test_dataset_name_sanitizes(full_name, expected):
    assert app._dataset_name(full_name) == expected


def test_dataset_name_is_api_safe():
    import re
    name = app._dataset_name("pg-!! crazy **Name** 123 ??")
    # lowercase alphanumerics + hyphens, starts alphanumeric, no doubles.
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
    assert "--" not in name
    assert not name.endswith("-")


def test_dataset_name_length_capped():
    assert len(app._dataset_name("pg-" + "x" * 500)) <= 100


def test_dataset_name_never_empty():
    # Degenerate input must still yield a usable name.
    assert app._dataset_name("") == "db"
    assert app._dataset_name("---") == "db"


# ---------------------------------------------------------------------------
# _backup_dataset_paths — mount path resolution by dataset NAME (not project)
# ---------------------------------------------------------------------------
def test_backup_dataset_paths_git_based(monkeypatch):
    monkeypatch.setenv("DOMINO_DATASETS_DIR", "/mnt/data")
    ds_name, snap_dir = app._backup_dataset_paths("pg-market")
    assert ds_name == "db-pg-market"
    # snapshot_dir is the dataset ROOT so the {DATASETS_DIR}/*/_dd_backup_config.json
    # scan in lifecycle.load_backup_override finds the marker one level down.
    assert snap_dir == "/mnt/data/db-pg-market"


def test_backup_dataset_paths_dfs(monkeypatch):
    monkeypatch.setenv("DOMINO_DATASETS_DIR", "/domino/datasets/local")
    ds_name, snap_dir = app._backup_dataset_paths("mongo-foo")
    assert ds_name == "db-mongo-foo"
    assert snap_dir == "/domino/datasets/local/db-mongo-foo"


def test_backup_dataset_path_is_scannable_by_lifecycle(monkeypatch, tmp_path):
    """The snapshot_dir must be a direct child of DOMINO_DATASETS_DIR so the
    lifecycle's {DATASETS_DIR}/*/_dd_backup_config.json glob can discover it."""
    monkeypatch.setenv("DOMINO_DATASETS_DIR", str(tmp_path))
    _, snap_dir = app._backup_dataset_paths("pg-market")
    assert Path(snap_dir).parent == tmp_path


# ---------------------------------------------------------------------------
# _config_pre_run_script — robust dual-location config delivery
# ---------------------------------------------------------------------------
def _extract_cfg(script: str) -> dict:
    enc = script.split("ENC='", 1)[1].split("'", 1)[0]
    return json.loads(base64.b64decode(enc).decode())


def test_pre_run_script_roundtrips_config():
    cfg = {"engine": "postgres", "db_id": "pg-market",
           "snapshot_dir": "/mnt/data/db-pg-market"}
    script = app._config_pre_run_script("pg-market", json.dumps(cfg))
    assert _extract_cfg(script) == cfg


def test_pre_run_script_writes_both_locations():
    script = app._config_pre_run_script("pg-market", json.dumps({"a": 1}))
    # 1. repo-relative fallback dir
    assert "/mnt/code/dbapps/pg-market.json" in script
    # 2. default-dataset _dd_configs dir (the durable, cross-project channel
    #    that lifecycle.find_config() searches as DBAPPS_DIR) — this is the
    #    one whose absence caused the "No config file found" boot failure.
    assert "_dd_configs" in script
    assert "${DOMINO_DATASETS_DIR:-/mnt/data}" in script
    assert "${DOMINO_PROJECT_NAME:-default}" in script
    assert 'DD_CFG_DIR/pg-market.json' in script


def test_pre_run_script_rewrites_launcher():
    script = app._config_pre_run_script("pg-market", json.dumps({"a": 1}))
    assert "dd-db-launcher.sh" in script
    assert "exec /opt/dd/app.sh" in script


def test_pre_run_script_default_dataset_write_is_best_effort():
    # A read-only default dataset must not abort boot — write is guarded.
    script = app._config_pre_run_script("pg-market", json.dumps({"a": 1}))
    cfg_write = [ln for ln in script.splitlines() if "DD_CFG_DIR/" in ln][0]
    assert "|| true" in cfg_write


# ---------------------------------------------------------------------------
# api_create_database — end-to-end create flow (SSE generator)
# ---------------------------------------------------------------------------
def _collect_sse(resp):
    """Parse an SSE Response body into a list of (event, data-dict)."""
    events = []
    raw = b"".join(resp.response).decode() if hasattr(resp, "response") else resp
    block_event = None
    for line in raw.splitlines():
        if line.startswith("event:"):
            block_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            payload = line[len("data:"):].strip()
            try:
                events.append((block_event, json.loads(payload)))
            except json.JSONDecodeError:
                events.append((block_event, {"_raw": payload}))
    return events


@pytest.fixture()
def wizard_client(monkeypatch):
    monkeypatch.setenv("DOMINO_DATASETS_DIR", "/mnt/data")
    app.app.config["TESTING"] = True
    return app.app.test_client()


@pytest.fixture()
def fake_domino(monkeypatch):
    """Stub the Domino API so the create flow runs without a live platform."""
    calls = {"create_dataset": [], "create_app": [], "start_app": [],
             "set_project_env_var": []}

    monkeypatch.setattr(dapi, "PROJECT_ID", "wizard-proj")
    monkeypatch.setattr(dapi, "PROJECT_NAME", "Database-Extension")
    monkeypatch.setattr(dapi, "PUBLIC_HOST", "https://cloud.example.com")
    monkeypatch.setattr(dapi, "list_apps", lambda *a, **k: [])

    def _create_dataset(name, project_id=""):
        calls["create_dataset"].append({"name": name, "project_id": project_id})
        return {"id": "ds-1", "name": name}

    def _create_app(**kwargs):
        calls["create_app"].append(kwargs)
        return {"id": "app-1", "currentVersion": {"id": "ver-1"}, "status": "Stopped"}

    def _start_app(app_id, **kwargs):
        calls["start_app"].append({"app_id": app_id, **kwargs})
        return {}

    def _get_app(app_id):
        return {"currentVersion": {"currentInstance": {"status": "Running"}}}

    def _set_env(project_id, name, value):
        calls["set_project_env_var"].append({"project_id": project_id, "name": name, "value": value})
        return {}

    monkeypatch.setattr(dapi, "create_dataset", _create_dataset)
    monkeypatch.setattr(dapi, "create_app", _create_app)
    monkeypatch.setattr(dapi, "start_app", _start_app)
    monkeypatch.setattr(dapi, "get_app", _get_app)
    monkeypatch.setattr(dapi, "set_project_env_var", _set_env)
    # Don't actually write the wizard-local fallback config to a real dataset.
    monkeypatch.setattr(app, "DBAPPS_DIR", Path("/tmp/dd-test-dbapps"))
    Path("/tmp/dd-test-dbapps").mkdir(parents=True, exist_ok=True)
    # Skip the 8s-per-attempt start wait.
    monkeypatch.setattr(app.time, "sleep", lambda *_a, **_k: None)
    return calls


def test_create_makes_dataset_in_target_project(wizard_client, fake_domino):
    resp = wizard_client.post("/api/databases", json={
        "engine": "postgres",
        "name": "market",
        "environmentId": "env-1",
        "hardwareTierId": "hw-1",
        "password": "secret",
        "projectId": "target-proj",
    })
    events = _collect_sse(resp)
    # The dataset is created exactly once, in the TARGET project, by name.
    assert len(fake_domino["create_dataset"]) == 1
    ds_call = fake_domino["create_dataset"][0]
    assert ds_call["project_id"] == "target-proj"
    assert ds_call["name"] == "db-pg-market"
    # And the create flow reached a terminal result event.
    kinds = [e for e, _ in events]
    assert "result" in kinds
    assert "error" not in kinds


def test_create_bakes_snapshot_dir_into_config(wizard_client, fake_domino):
    resp = wizard_client.post("/api/databases", json={
        "engine": "postgres", "name": "market",
        "environmentId": "env-1", "hardwareTierId": "hw-1",
        "password": "secret", "projectId": "target-proj",
    })
    _collect_sse(resp)  # consume the stream so the generator runs
    # The app is created with a pre-run script carrying the config; the config
    # must include snapshot_dir pointing at the dedicated dataset's mount.
    create_kwargs = fake_domino["create_app"][0]
    cfg = _extract_cfg(create_kwargs["pre_run_script"])
    assert cfg["snapshot_dir"] == "/mnt/data/db-pg-market"
    assert cfg["db_id"] == "pg-market"
    # App is created in the target project too.
    assert create_kwargs["project_id"] == "target-proj"


def test_create_sets_snapshot_env_var_to_dataset(wizard_client, fake_domino):
    resp = wizard_client.post("/api/databases", json={
        "engine": "postgres", "name": "market",
        "environmentId": "env-1", "hardwareTierId": "hw-1",
        "password": "secret", "projectId": "target-proj",
    })
    _collect_sse(resp)  # consume the stream so the generator runs
    env_calls = fake_domino["set_project_env_var"]
    assert env_calls, "expected the snapshot env var to be set on the target project"
    snap = env_calls[0]
    assert snap["project_id"] == "target-proj"
    assert snap["name"] == "DD_SNAPSHOT_PG_MARKET"
    assert snap["value"] == "/mnt/data/db-pg-market"


def test_create_survives_dataset_failure(wizard_client, fake_domino, monkeypatch):
    """A dataset-creation error must NOT abort DB creation — the DB should
    still be created and the user warned to configure backups later."""
    def _boom(name, project_id=""):
        raise dapi.DominoApiError("POST", "/datasets", 403, "forbidden")
    monkeypatch.setattr(dapi, "create_dataset", _boom)

    resp = wizard_client.post("/api/databases", json={
        "engine": "postgres", "name": "market",
        "environmentId": "env-1", "hardwareTierId": "hw-1",
        "password": "secret", "projectId": "target-proj",
    })
    events = _collect_sse(resp)
    kinds = [e for e, _ in events]
    # Warned, but still created and not fatal.
    assert "warn" in kinds
    assert "result" in kinds
    assert "error" not in kinds
    assert len(fake_domino["create_app"]) == 1


def test_create_reuses_existing_dataset(wizard_client, fake_domino, monkeypatch):
    """A 409/already-exists is treated as success (reuse), not a warning."""
    def _conflict(name, project_id=""):
        raise dapi.DominoApiError("POST", "/datasets", 409, "dataset already exists")
    monkeypatch.setattr(dapi, "create_dataset", _conflict)

    resp = wizard_client.post("/api/databases", json={
        "engine": "postgres", "name": "market",
        "environmentId": "env-1", "hardwareTierId": "hw-1",
        "password": "secret", "projectId": "target-proj",
    })
    events = _collect_sse(resp)
    dataset_oks = [d for e, d in events
                   if e == "ok" and "already exists" in d.get("msg", "")]
    assert dataset_oks, "409 should surface as a reuse 'ok', not a warn"
