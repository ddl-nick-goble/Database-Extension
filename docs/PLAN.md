# Domino Databases — Plan (v4)

## Goal

Let a Domino user click a few buttons and get a real Postgres or MongoDB instance running in their project, reachable from their laptop via the native client (`psql`, JDBC, ODBC, `mongosh`). Snapshots persist to a Domino Dataset.

## Architecture (v4 — App-hosted, current)

Every database is a **Domino App**. The wizard, also a Domino App, provisions DB Apps via the Apps API.

```
┌─────────────────────────────────────────────────────────────────────┐
│ LAPTOP                                                              │
│   psql / DBeaver / Tableau / Python                                  │
│       ↓ connects to localhost:5432                                   │
│   `domino-db tunnel pg-myfirst`  (~150 LoC Python; v1: Go binary)    │
│       ↓ wss://.../app/pg-myfirst/wire   + X-Domino-Api-Key           │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
                  Domino reverse proxy
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│ DOMINO APP: pg-myfirst    (env: dd-postgres-app)                     │
│                                                                      │
│   :8888  Flask router (dbapp/router.py) — the ONLY external port    │
│     ├── GET  /              → branded status page (HTML)             │
│     ├── WS   /wire          → relay → 127.0.0.1:5432                 │
│     ├── *    /admin/*       → reverse-proxy → 127.0.0.1:8978         │
│     ├── GET  /api/*         → JSON status/config                     │
│     └── GET  /healthz       → engine liveness                        │
│                                                                      │
│   :5432  Postgres (internal)   data: /mnt/db/pgdata                  │
│   :8978  CloudBeaver (internal) preconfigured to local PG            │
│   cron   snapshotter (every 60m) → /domino/datasets/db-pg-myfirst/   │
│                                                                      │
│   On boot: lifecycle.py restores from latest dataset snapshot         │
│            (or initializes fresh) before launching the router.       │
└─────────────────────────────────────────────────────────────────────┘
                              ↑ provisions via /api/apps/beta/apps
┌─────────────────────────────────────────────────────────────────────┐
│ DOMINO APP: dd-wizard    (env: standard Python env)                  │
│   :8888  Flask + static SPA — React/MRM-Portal-style UI              │
│   POST /api/databases → writes /mnt/code/dbapps/<name>.json,         │
│                          then POST /api/apps/beta/apps               │
└─────────────────────────────────────────────────────────────────────┘
```

## Why App, not Workspace

| | Workspace | App (current) |
|---|---|---|
| Lifecycle | Interactive — idles out (admin flag is a workaround) | Always-on service |
| Multi-port externally | Only via JupyterLab+server-proxy | Single port multiplexed by Flask (cleaner; ~80 LoC) |
| "Open and see your DB" | Workspace IDE = JupyterLab + extra tab for DB explorer | App URL **is** the DB explorer (CloudBeaver at `/admin/`) |
| Discoverability | In the project's workspaces list | In the Apps catalog ("your databases") |
| Restart on failure | Workspace pause semantics — bad for a DB | App supervisor — built in |
| Mental model | "Workspace running a database" (wrong) | "Database (which happens to be an app)" |

## Dispatcher pattern

All Apps in the project share `/mnt/code/app.sh`. The compute environment image sets `ENV DD_ROLE=postgres|mongo|wizard`; `app.sh` dispatches:

```bash
case "$DD_ROLE" in
    postgres|mongo) exec bash /mnt/code/dbapp/app.sh ;;  # DB app
    wizard|"")      exec python /mnt/code/app.py     ;;  # the wizard
esac
```

## Per-DB config & secrets

Before creating a DB App, the wizard writes `/mnt/code/dbapps/<app-name>.json` with `{engine, db_id, password, user, port, ...}`. The file is gitignored. The DB App's `lifecycle.find_config()` reads it by app name (with a most-recent-file fallback in case `DOMINO_APP_NAME` isn't injected).

**v1 will replace this** with a proper secret store (Domino-provided per-app secrets, or AWS Secrets Manager via IRSA). The on-disk approach is acceptable for the spike but isn't a long-term answer.

## Wire-protocol tunnel

Identical to v3's design — byte-transparent WS↔TCP relay, Neon-style. The `/wire` endpoint is now part of the DB App's Flask router on port 8888 instead of a separate `:8765` port behind `jupyter-server-proxy`. Cleaner.

```
laptop psql ↔ 127.0.0.1:5432 (CLI listener)
            ↔ WSS /app/pg-myfirst/wire (Domino proxy)
            ↔ Flask router /wire endpoint
            ↔ socket to 127.0.0.1:5432 (real Postgres)
```

## Snapshot strategy (unchanged)

| | Postgres | Mongo |
|---|---|---|
| Engine writes to | `/mnt/db/pgdata` | `/mnt/db/mongo` |
| Snapshot tool | `pg_basebackup -Ft -z -Xs` + WAL archive | `mongodump --oplog --gzip` |
| Cadence | hourly | hourly |
| Target | `/domino/datasets/db-<name>/snapshots/<ts>/` | same |
| Retention | 6 hourly + 7 daily + 4 weekly | same |
| Restore | on cold start, `lifecycle.restore_or_init_*()` | same |

## Open risks

1. **`DOMINO_APP_NAME` may not be injected** — the lifecycle has a fallback (most-recent config file), but we should confirm and prefer the explicit path.
2. **CloudBeaver under a `/admin/` path prefix** — needs verification that its assets emit relative URLs. If not, the reverse proxy needs to rewrite. Spike check.
3. **Flask-sock + gunicorn worker class** — `gthread` is the path of least resistance; might need to switch to `geventwebsocket.worker.GeventWebSocketWorker` if streaming throughput suffers.

## What's still to come

- Re-test the wizard at `/proxy/8501/` after the rewrite (should still work — only `domino_api.py` and `app.py` shape changed; HTML/CSS/JS unchanged).
- End-to-end exercise: wizard creates `pg-myfirst` → app boots → user opens its URL → CloudBeaver loads → `psql` from laptop works via tunnel.
- Replace on-disk secret with a real secret store.
- Ship Go binary for the laptop CLI.
