# snapshotter

In-container cron sidecar that captures the live database to the project's Dataset on a schedule, so the DB survives workspace restarts and accidents.

| File | What |
|---|---|
| `snapshot_postgres.py` | `pg_basebackup -Ft -z -Xs` → `/domino/datasets/db-<id>/snapshots/<ts>/basebackup/`, then triggers a Dataset snapshot via Domino API. Tiered retention: 6 hourly + 7 daily + 4 weekly. |
| `snapshot_mongo.py` | `mongodump --oplog --gzip` → same layout. |

Both are mounted into the env image at `/opt/dd/snapshotter/` (via the Dockerfile `COPY` line) and scheduled by `preRun.sh` (`crontab` line near the bottom).

Restore is **not** in this directory — it lives inline in `envs/dd-*-env/preRun.sh`, because it has to run before Postgres/Mongo start.

## Knobs (env vars)

| Var | Default | Notes |
|---|---|---|
| `DD_DB_ID` | `$DOMINO_RUN_ID` | Per-DB snapshot directory key. |
| `DD_SNAPSHOT_INTERVAL_MIN` | `60` | Cron cadence in minutes (set in preRun.sh crontab line). |
| `DD_PG_PASSWORD` / `DD_MONGO_PASSWORD` | — | Required; engine admin password. |
| `DOMINO_USER_API_KEY` | (auto) | Injected by Domino; used to call the Datasets API. |

## Failure modes worth knowing

- **Snapshot taken, dataset API down.** The on-disk basebackup is still durable; we just don't get a Domino Dataset version. Re-runs aren't an issue — each tries the API independently.
- **`pg_basebackup` interrupted.** The partial dir is removed; next run takes a clean one.
- **Default Dataset snapshot cap is 20** (admin-configurable). The tiered retention here keeps the local on-disk count at 17 max; combined with periodic Dataset snapshots you'll have a healthy backup window without ever hitting the cap.
