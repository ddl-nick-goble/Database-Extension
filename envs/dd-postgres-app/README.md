# dd-postgres-env

The Domino compute environment that turns a workspace into a Postgres database.

## What's inside

The workspace IDE is **JupyterLab** — not CloudBeaver. This is intentional: Domino's `/proxy/<port>/` routing is provided by Jupyter's `jupyter-server-proxy` extension, so JupyterLab must be the front-of-pod process. Everything else runs as a background sidecar:

| Port | Service | Reach |
|---|---|---|
| 8888 | JupyterLab (the IDE — has jupyter-server-proxy) | workspace URL root |
| 8978 | CloudBeaver (DB explorer, auto-connected to local PG) | `…/proxy/8978/` |
| 8765 | ws2tcp (WS↔TCP relay for psql/JDBC/ODBC tunnel)        | `…/proxy/8765/wire` |
| 5432 | Postgres (internal only — not externally reachable)   | (none) |

- **Snapshotter** (`/mnt/code/snapshotter/snapshot_postgres.py`) — cron-driven `pg_basebackup` + WAL archive into the project's Dataset
- **preRun.sh** (`envs/dd-postgres-env/preRun.sh`, called via a stub at `/var/opt/domino/preRun.sh`) — restores from latest snapshot on cold start, boots Postgres + ws2tcp + CloudBeaver + cron, then exits so JupyterLab starts

## How to use it (manual, for the Day 1–2 spike)

1. In Domino UI → **Environments** → **Create Environment**
   - Base image: latest Domino Standard Environment (Ubuntu 22.04, Py 3.10)
   - Paste the contents of `Dockerfile` into "Dockerfile Instructions"
   - Paste the contents of `workspaceTools.yaml` into "Pluggable Workspace Tools"
   - Build.
2. On the workspace launch form, set environment variables:
   - `DD_PG_PASSWORD` — admin password for the DB
   - `DD_DB_ID` (optional) — stable identifier; defaults to the run ID
3. Pick "JupyterLab + DB sidecars" as the IDE (the only choice).
4. Launch. preRun starts Postgres + ws2tcp + CloudBeaver; JupyterLab loads on `:8888`.
5. From the workspace URL: open `…/proxy/8978/` in a new tab → CloudBeaver, pre-connected to local Postgres.
6. From your laptop: `domino-db tunnel <run-id>` → `psql -h 127.0.0.1` should work.

## Required env vars

| Var | Purpose | Default |
|---|---|---|
| `DD_PG_PASSWORD` | Postgres admin password | **required** |
| `DD_PG_USER` | Postgres admin user | `domino` |
| `DD_PG_PORT` | Postgres listen port (inside pod) | `5432` |
| `DD_WS_PORT` | ws2tcp listen port | `8765` |
| `DD_DB_ID` | Snapshot directory key | `$DOMINO_RUN_ID` |
| `DD_SNAPSHOT_INTERVAL_MIN` | Cron cadence | `60` |
