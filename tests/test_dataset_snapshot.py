"""Tests for the shared snapshotter dataset-snapshot helper.

Covers the fix that moved all four engine snapshotters onto the v1 snapshot API
and made them target the DEDICATED per-DB dataset (by name) instead of the
project's default dataset — and tolerate the legacy shared-dataset layout.
"""
import sys
from pathlib import Path

import pytest

# The snapshotters import `_dataset_snapshot` as a sibling module (sys.path[0]
# is the script dir when run standalone). Mirror that here.
SNAP_DIR = Path(__file__).resolve().parent.parent / "snapshotter"
sys.path.insert(0, str(SNAP_DIR))

import _dataset_snapshot as ds  # noqa: E402


# ---------------------------------------------------------------------------
# resolve_dataset_target
# ---------------------------------------------------------------------------
def test_resolve_dedicated_dataset_root(tmp_path):
    """New layout: snapshot_root IS the dataset root → version its durable
    top-level entries, skipping transient staging dirs."""
    base = tmp_path
    root = base / "db-pg-cod"
    (root / "basebackup").mkdir(parents=True)
    (root / "wal").mkdir()
    (root / "_diag").mkdir()
    (root / "basebackup.new").mkdir()   # transient — must be skipped
    (root / "basebackup.old").mkdir()   # transient — must be skipped

    name, rel = ds.resolve_dataset_target(root, str(base))
    assert name == "db-pg-cod"
    assert rel == ["_diag", "basebackup", "wal"]
    assert "basebackup.new" not in rel and "basebackup.old" not in rel


def test_resolve_legacy_subdir(tmp_path):
    """Legacy / manual layout: snapshot_root is a db-<id>/ subdir within a
    shared dataset → version exactly that subpath."""
    base = tmp_path
    root = base / "market-data" / "db-pg-cod"
    root.mkdir(parents=True)
    name, rel = ds.resolve_dataset_target(root, str(base))
    assert name == "market-data"
    assert rel == ["db-pg-cod"]


def test_resolve_deeply_nested_subdir(tmp_path):
    base = tmp_path
    root = base / "shared" / "a" / "b"
    root.mkdir(parents=True)
    name, rel = ds.resolve_dataset_target(root, str(base))
    assert name == "shared"
    assert rel == ["a/b"]


def test_resolve_outside_datasets_dir(tmp_path):
    name, rel = ds.resolve_dataset_target(Path("/somewhere/else"), str(tmp_path))
    assert name == ""
    assert rel == []


def test_resolve_root_with_only_transient_is_empty(tmp_path):
    base = tmp_path
    root = base / "db-pg-cod"
    (root / "basebackup.new").mkdir(parents=True)
    name, rel = ds.resolve_dataset_target(root, str(base))
    assert name == "db-pg-cod"
    assert rel == []  # nothing durable to snapshot yet


def test_resolve_root_is_datasets_dir_itself(tmp_path):
    name, rel = ds.resolve_dataset_target(tmp_path, str(tmp_path))
    assert name == ""
    assert rel == []


# ---------------------------------------------------------------------------
# Fakes for the httpx surface
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Records GET/POST calls and returns scripted responses."""
    def __init__(self, get_resp=None, post_resp=None):
        self._get_resp = get_resp
        self._post_resp = post_resp
        self.get_calls = []
        self.post_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        self.get_calls.append((url, params))
        return self._get_resp

    def post(self, url, json=None):
        self.post_calls.append((url, json))
        return self._post_resp


_TWO_DATASETS = {
    "datasets": [
        {"dataset": {"id": "default-id", "name": "market-data", "projectId": "P1"}},
        {"dataset": {"id": "dedicated-id", "name": "db-pg-cod", "projectId": "P1"}},
        {"dataset": {"id": "other-proj", "name": "db-pg-cod", "projectId": "P2"}},
    ]
}


# ---------------------------------------------------------------------------
# find_dataset_id
# ---------------------------------------------------------------------------
def test_find_dataset_id_matches_name_and_project():
    c = FakeClient(get_resp=FakeResp(payload=_TWO_DATASETS))
    assert ds.find_dataset_id(c, "P1", "db-pg-cod") == "dedicated-id"
    # Uses the v2 listing endpoint with projectIdsToInclude.
    url, params = c.get_calls[0]
    assert url == "/api/datasetrw/v2/datasets"
    assert params == {"projectIdsToInclude": "P1"}


def test_find_dataset_id_does_not_cross_projects():
    c = FakeClient(get_resp=FakeResp(payload=_TWO_DATASETS))
    # Same name exists in P2, but we asked for P1's dedicated dataset only.
    assert ds.find_dataset_id(c, "P1", "nonexistent") is None


# ---------------------------------------------------------------------------
# trigger_domino_snapshot
# ---------------------------------------------------------------------------
@pytest.fixture()
def logs():
    out = []
    return out, (lambda m: out.append(m))


def test_trigger_skips_without_credentials(logs):
    out, log = logs
    ds.trigger_domino_snapshot(api_host="", api_key="", project_id="",
                               snapshot_root="/x", datasets_dir="/mnt/data", log=log)
    assert any("skipping" in m for m in out)


def test_trigger_skips_when_outside_datasets_dir(logs, tmp_path):
    out, log = logs
    ds.trigger_domino_snapshot(api_host="http://h", api_key="k", project_id="P1",
                               snapshot_root="/not/under/data",
                               datasets_dir=str(tmp_path), log=log)
    assert any("not under datasets dir" in m for m in out)


def test_trigger_skips_when_no_durable_content(logs, tmp_path):
    out, log = logs
    root = tmp_path / "db-pg-cod"
    root.mkdir()
    ds.trigger_domino_snapshot(api_host="http://h", api_key="k", project_id="P1",
                               snapshot_root=root, datasets_dir=str(tmp_path), log=log)
    assert any("no durable content" in m for m in out)


def test_trigger_posts_v1_snapshot_for_dedicated_dataset(monkeypatch, logs, tmp_path):
    out, log = logs
    root = tmp_path / "db-pg-cod"
    (root / "basebackup").mkdir(parents=True)
    (root / "wal").mkdir()

    post_resp = FakeResp(status=201, payload={"snapshot": {"id": "snap-9", "version": 3,
                                                           "lifecycleStatus": "Active"}})
    client = FakeClient(get_resp=FakeResp(payload=_TWO_DATASETS), post_resp=post_resp)
    monkeypatch.setattr(ds.httpx, "Client", lambda **kw: client)

    ds.trigger_domino_snapshot(api_host="http://h", api_key="k", project_id="P1",
                               snapshot_root=root, datasets_dir=str(tmp_path), log=log)

    # Posted to the v1 per-dataset snapshots endpoint for the DEDICATED dataset.
    assert client.post_calls, "expected a snapshot POST"
    url, body = client.post_calls[0]
    assert url == "/api/datasetrw/v1/datasets/dedicated-id/snapshots"
    assert body == {"relativeFilePaths": ["basebackup", "wal"]}
    assert any("snap-9" in m for m in out)


def test_trigger_skips_when_dataset_not_yet_mounted(monkeypatch, logs, tmp_path):
    """Freshly created dataset isn't in the listing until the App restarts —
    must skip gracefully, not crash."""
    out, log = logs
    root = tmp_path / "db-not-listed"
    (root / "basebackup").mkdir(parents=True)
    client = FakeClient(get_resp=FakeResp(payload=_TWO_DATASETS))
    monkeypatch.setattr(ds.httpx, "Client", lambda **kw: client)

    ds.trigger_domino_snapshot(api_host="http://h", api_key="k", project_id="P1",
                               snapshot_root=root, datasets_dir=str(tmp_path), log=log)
    assert not client.post_calls
    assert any("not found" in m and "next restart" in m for m in out)


def test_trigger_handles_already_in_progress(monkeypatch, logs, tmp_path):
    out, log = logs
    root = tmp_path / "db-pg-cod"
    (root / "basebackup").mkdir(parents=True)
    client = FakeClient(
        get_resp=FakeResp(payload=_TWO_DATASETS),
        post_resp=FakeResp(status=400, text="a snapshot is already in progress"),
    )
    monkeypatch.setattr(ds.httpx, "Client", lambda **kw: client)

    ds.trigger_domino_snapshot(api_host="http://h", api_key="k", project_id="P1",
                               snapshot_root=root, datasets_dir=str(tmp_path), log=log)
    assert any("already in progress" in m for m in out)


def test_trigger_never_raises_on_network_error(monkeypatch, logs, tmp_path):
    out, log = logs
    root = tmp_path / "db-pg-cod"
    (root / "basebackup").mkdir(parents=True)

    def _boom(**kw):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(ds.httpx, "Client", _boom)

    # Must not propagate — snapshotter loop has to survive.
    ds.trigger_domino_snapshot(api_host="http://h", api_key="k", project_id="P1",
                               snapshot_root=root, datasets_dir=str(tmp_path), log=log)
    assert any("failed" in m for m in out)
