"""Shared Domino dataset-snapshot helper for all engine snapshotters.

Every engine snapshotter writes its durable backup into a Domino Dataset and
then asks Domino to capture a *versioned* snapshot of it (visible in the dataset
UI under "Snapshots"). That second step is identical across engines, so it lives
here — one implementation to get right, one place to test.

Two on-disk layouts are supported transparently:

  - Dedicated per-DB dataset (current): DD_SNAPSHOT_DIR is the dataset ROOT,
    e.g. /mnt/data/db-<id>. We version the durable top-level dirs inside it.
  - Shared dataset + db-<id>/ subdir (legacy / manual "path" mode):
    DD_SNAPSHOT_DIR is /mnt/data/<dataset>/db-<id>; we version that subpath.

API notes (verified against this Domino's public swagger):
  - List:   GET  /api/datasetrw/v2/datasets?projectIdsToInclude=<pid>
            (the v2 listing is the one exposed; server-side projectId filtering
            is unreliable, so we always filter by name + projectId client-side)
  - Create: POST /api/datasetrw/v1/datasets/<datasetId>/snapshots
            body {relativeFilePaths: [...]} — the old /v4/datasetrw/snapshot
            path is not exposed on this build and 404s.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import httpx


def _is_transient(name: str) -> bool:
    """Staging/working dirs the snapshotters create at the dataset root
    (basebackup.new, basebackup.old, *dump.new.<ts>, bgsave.new.<ts>,
    latest.new) — never worth versioning."""
    return ".new" in name or ".old" in name


def resolve_dataset_target(snapshot_root, datasets_dir: str) -> tuple[str, list[str]]:
    """Map an absolute snapshot dir to (dataset_name, relative_file_paths).

    dataset_name is the first path component under datasets_dir (datasets mount
    at <datasets_dir>/<dataset-name>/...). relative_file_paths is what to ask
    Domino to snapshot, relative to the dataset root:

      - snapshot_root deeper than the dataset root -> [that subpath]
      - snapshot_root IS the dataset root          -> its durable top-level
        entries (transient staging dirs skipped)

    Returns ("", []) when snapshot_root is not under datasets_dir.
    """
    snapshot_root = Path(snapshot_root)
    try:
        parts = snapshot_root.relative_to(Path(datasets_dir)).parts
    except ValueError:
        return "", []
    if not parts:
        return "", []
    dataset_name = parts[0]
    if len(parts) > 1:
        return dataset_name, ["/".join(parts[1:])]
    # snapshot_root IS the dataset root — version its durable contents. The API
    # snapshots named entries, so we enumerate them rather than passing ".".
    try:
        entries = sorted(
            p.name for p in snapshot_root.iterdir() if not _is_transient(p.name)
        )
    except OSError:
        entries = []
    return dataset_name, entries


def find_dataset_id(client: httpx.Client, project_id: str, dataset_name: str) -> str | None:
    """Resolve the dataset ID for the dataset named `dataset_name` in this
    project. Filters by name + projectId client-side (projectId query filtering
    is ignored on some builds, so we never trust server-side filtering)."""
    r = client.get("/api/datasetrw/v2/datasets",
                   params={"projectIdsToInclude": project_id})
    r.raise_for_status()
    for wrapped in r.json().get("datasets", []):
        ds = wrapped.get("dataset", wrapped)
        if ds.get("projectId") == project_id and ds.get("name") == dataset_name:
            return ds.get("id")
    return None


def trigger_domino_snapshot(
    *,
    api_host: str,
    api_key: str,
    project_id: str,
    snapshot_root,
    datasets_dir: str,
    log: Callable[[str], None],
) -> None:
    """Capture a versioned Domino snapshot of the backup the caller just wrote
    to `snapshot_root`. Best-effort: every failure is logged, never raised — a
    missing version must not crash the snapshotter loop."""
    if not (api_host and api_key and project_id):
        log("no API credentials / project id — skipping Domino snapshot")
        return
    dataset_name, rel_paths = resolve_dataset_target(snapshot_root, datasets_dir)
    if not dataset_name:
        log(f"{snapshot_root} is not under datasets dir {datasets_dir} — "
            f"skipping Domino snapshot")
        return
    if not rel_paths:
        log(f"no durable content under {snapshot_root} yet — skipping Domino snapshot")
        return
    try:
        with httpx.Client(base_url=api_host,
                          headers={"X-Domino-Api-Key": api_key},
                          timeout=30) as c:
            ds_id = find_dataset_id(c, project_id, dataset_name)
            if not ds_id:
                log(f"dataset named {dataset_name!r} not found in project "
                    f"{project_id} — skipping (a freshly-created dataset mounts "
                    f"on the App's next restart)")
                return
            r = c.post(f"/api/datasetrw/v1/datasets/{ds_id}/snapshots",
                       json={"relativeFilePaths": rel_paths})
            if r.status_code in (200, 201):
                snap = (r.json() or {}).get("snapshot", {})
                log(f"Domino snapshot created id={snap.get('id')} "
                    f"version={snap.get('version')} "
                    f"status={snap.get('lifecycleStatus')} paths={rel_paths}")
            elif r.status_code == 400 and "already in progress" in r.text:
                log("Domino snapshot already in progress — will catch the next tick")
            else:
                log(f"Domino snapshot API {r.status_code}: {r.text[:300]}")
    except Exception as e:
        log(f"Domino snapshot trigger failed: {e}")
