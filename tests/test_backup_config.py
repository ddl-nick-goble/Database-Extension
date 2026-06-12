"""Tests for the backup config persistence and override mechanism."""
import json
import sys
from pathlib import Path
import pytest

# Ensure the repo root is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def tmp_cfg(tmp_path):
    return {
        "db_id": "test-db-001",
        "engine": "postgres",
        "port": 5432,
        "password": "secret",
        "user": "domino",
    }


@pytest.fixture(autouse=True)
def clean_override(tmp_path, monkeypatch):
    """Point the in-memory override file at a temp path so tests are isolated."""
    override = tmp_path / "dd-backup-override.json"
    import dbapp.lifecycle as lc
    monkeypatch.setattr(lc, "_BACKUP_OVERRIDE", override)
    yield override


# ---------------------------------------------------------------------------
# save_backup_config
# ---------------------------------------------------------------------------
def test_save_backup_config_writes_tmp(tmp_path, tmp_cfg, clean_override):
    from dbapp.lifecycle import save_backup_config
    snap_dir = tmp_path / "backups" / f"db-{tmp_cfg['db_id']}"
    save_backup_config(tmp_cfg, snap_dir)

    assert clean_override.exists()
    data = json.loads(clean_override.read_text())
    assert data["db_id"] == tmp_cfg["db_id"]
    assert data["snapshot_dir"] == str(snap_dir)


def test_save_backup_config_writes_persistent(tmp_path, tmp_cfg):
    from dbapp.lifecycle import save_backup_config
    snap_dir = tmp_path / "backups" / f"db-{tmp_cfg['db_id']}"
    save_backup_config(tmp_cfg, snap_dir)

    persistent = snap_dir / "_dd_backup_config.json"
    assert persistent.exists()
    data = json.loads(persistent.read_text())
    assert data["db_id"] == tmp_cfg["db_id"]
    assert data["snapshot_dir"] == str(snap_dir)
    assert "configured_at" in data


def test_save_backup_config_creates_snapshot_dir(tmp_path, tmp_cfg):
    from dbapp.lifecycle import save_backup_config
    snap_dir = tmp_path / "deep" / "nested" / f"db-{tmp_cfg['db_id']}"
    assert not snap_dir.exists()
    save_backup_config(tmp_cfg, snap_dir)
    assert snap_dir.exists()


# ---------------------------------------------------------------------------
# load_backup_override — in-memory path
# ---------------------------------------------------------------------------
def test_load_backup_override_from_tmp(tmp_path, tmp_cfg, clean_override):
    from dbapp.lifecycle import save_backup_config, load_backup_override
    snap_dir = tmp_path / "backups" / f"db-{tmp_cfg['db_id']}"
    save_backup_config(tmp_cfg, snap_dir)

    result = load_backup_override(tmp_cfg)
    assert result["snapshot_dir"] == str(snap_dir)


def test_load_backup_override_ignores_wrong_db_id(tmp_path, tmp_cfg, clean_override):
    from dbapp.lifecycle import load_backup_override
    clean_override.write_text(json.dumps({"db_id": "other-db", "snapshot_dir": "/mnt/other"}))
    result = load_backup_override(tmp_cfg)
    # Should not have modified snapshot_dir for a different db_id.
    assert result.get("snapshot_dir") is None


# ---------------------------------------------------------------------------
# load_backup_override — persistent scan path
# ---------------------------------------------------------------------------
def test_load_backup_override_from_dataset_scan(tmp_path, tmp_cfg, monkeypatch):
    from dbapp import lifecycle as lc
    # Simulate a datasets dir with one dataset containing a backup config.
    # The lifecycle scan looks for {DATASETS_DIR}/*/_dd_backup_config.json,
    # so the config lives directly inside each dataset dir.
    datasets_dir = tmp_path / "mnt" / "data"
    ds_dir = datasets_dir / "my-backup-dataset"
    ds_dir.mkdir(parents=True)
    snap_dir = ds_dir / f"db-{tmp_cfg['db_id']}"
    snap_dir.mkdir()
    # Place the config at ds_dir/_dd_backup_config.json (not inside snap_dir).
    (ds_dir / "_dd_backup_config.json").write_text(json.dumps({
        "db_id": tmp_cfg["db_id"],
        "snapshot_dir": str(snap_dir),
    }))

    monkeypatch.setenv("DOMINO_DATASETS_DIR", str(datasets_dir))

    result = lc.load_backup_override(tmp_cfg)
    assert result["snapshot_dir"] == str(snap_dir)


def test_load_backup_override_no_config(tmp_path, tmp_cfg, monkeypatch):
    from dbapp import lifecycle as lc
    datasets_dir = tmp_path / "mnt" / "data"
    datasets_dir.mkdir(parents=True)
    monkeypatch.setenv("DOMINO_DATASETS_DIR", str(datasets_dir))
    result = lc.load_backup_override(tmp_cfg)
    # No override — cfg unchanged.
    assert result.get("snapshot_dir") is None


# ---------------------------------------------------------------------------
# Router API endpoints
# ---------------------------------------------------------------------------
@pytest.fixture()
def router_app(tmp_path, monkeypatch, tmp_cfg):
    """Bootstrap the router Flask app with a minimal config.

    router.py raises at import time if /tmp/dd-config.json is missing, so we
    write it before importing the module. If it has already been imported (e.g.
    by a prior test) we monkeypatch CFG directly.
    """
    import importlib

    monkeypatch.setenv("DOMINO_DATASETS_DIR", str(tmp_path / "mnt" / "data"))
    monkeypatch.setenv("DOMINO_PROJECT_ID", "proj-test-123")

    # Write /tmp/dd-config.json so the module-level guard passes on first import.
    config_cache = Path("/tmp/dd-config.json")
    config_cache.write_text(json.dumps(tmp_cfg))

    import dbapp.lifecycle as lc
    monkeypatch.setattr(lc, "_BACKUP_OVERRIDE", tmp_path / "backup-override.json")

    import dbapp.router as router_mod
    monkeypatch.setattr(router_mod, "CFG", dict(tmp_cfg))
    router_mod.app.config["TESTING"] = True
    return router_mod.app.test_client()


def test_backup_status_endpoint(tmp_path, router_app, monkeypatch):
    monkeypatch.setenv("DOMINO_DATASETS_DIR", str(tmp_path))
    resp = router_app.get("/api/backup/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "snapshot_dir" in data
    assert "last_snapshot" in data


def test_backup_configure_missing_path(router_app):
    resp = router_app.post("/api/backup/configure",
                           json={"mode": "path", "path": ""},
                           content_type="application/json")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_backup_configure_nonexistent_path(router_app):
    resp = router_app.post("/api/backup/configure",
                           json={"mode": "path", "path": "/nonexistent/path/xyz"},
                           content_type="application/json")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "does not exist" in data["error"]


def test_backup_configure_valid_path(tmp_path, router_app, monkeypatch):
    import dbapp.lifecycle as lc
    monkeypatch.setattr(lc, "_BACKUP_OVERRIDE", tmp_path / "override.json")

    dataset_dir = tmp_path / "my-dataset"
    dataset_dir.mkdir()
    resp = router_app.post("/api/backup/configure",
                           json={"mode": "path", "path": str(dataset_dir)},
                           content_type="application/json")
    data = resp.get_json()
    assert resp.status_code == 200, data
    assert data["status"] == "ok"
    assert "snapshot_dir" in data
    # Persistent config should be written
    snap_dir = Path(data["snapshot_dir"])
    assert (snap_dir / "_dd_backup_config.json").exists()


def test_backup_configure_missing_dataset_name(router_app):
    resp = router_app.post("/api/backup/configure",
                           json={"mode": "create", "name": ""},
                           content_type="application/json")
    assert resp.status_code == 400
