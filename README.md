# Domino Databases

A Domino extension that lets users provision real database instances (Postgres, MongoDB) inside their Domino project with a few clicks. Each database is a long-running **Domino App** that runs the real engine — plus a wire-protocol tunnel so `psql`, JDBC, ODBC, and `mongosh` work unmodified against `localhost`.

```
laptop                       Domino                                          
                                                                            
psql ──► 127.0.0.1:5432      /<owner>/<proj>/app/pg-myfirst/wire             
         │                       │                                          
   domino-db CLI ─── wss:// ─────► Flask router :8888 ──► Postgres :5432    
                                       │                                    
                                       ├─► /admin/ → CloudBeaver :8978      
                                       ├─► /      → status page             
                                       └─► /api/  → JSON status              
                                   /mnt/db/pgdata                            
                                                                            
                                   cron: hourly pg_basebackup ─► /domino/datasets/db-pg-myfirst/
```

## Repo layout

| Path | What |
|---|---|
| `app.sh` | Root dispatcher — runs the wizard *or* the DB-app router depending on `DD_ROLE` (set by the env image) |
| `app.py` + `static/` + `domino_api.py` | The **wizard** — Flask + static SPA. Provisions Database Apps via the Apps API. |
| `dbapp/` | The **DB App** — Flask router (`router.py`), lifecycle/boot logic (`lifecycle.py`), entrypoint (`app.sh`) |
| `envs/dd-postgres-app/` | Compute environment for a Postgres DB App (Dockerfile instructions, README) |
| `envs/dd-mongo-app/` | Same for MongoDB |
| `ws2tcp/server.py` | Standalone WS↔TCP relay (kept around for reference; production runs inside `dbapp/router.py`) |
| `snapshotter/` | Cron-driven `pg_basebackup` / `mongodump --oplog` → Domino Dataset |
| `client/domino_db.py` | Laptop CLI that opens the WS tunnel and exposes a local TCP listener |
| `dbapps/` | (gitignored) per-DB config files written by the wizard |
| `docs/PLAN.md` | Full architecture rationale and open risks |

See [docs/PLAN.md](docs/PLAN.md) for the design rationale.

## Quick start

1. Build `envs/dd-postgres-app/` as a Domino environment (paste the Dockerfile, leave workspaceTools blank, build).
2. Set `DD_POSTGRES_ENV_ID` (and optionally `DD_MONGO_ENV_ID`) as env vars on the wizard app/workspace.
3. Run the wizard: `PORT=8501 bash app.sh` (dev, in a workspace) or deploy as a Domino App (prod).
4. In the wizard, click **+ New Database** → pick Postgres → fill in name + password → Provision.
5. The wizard creates a DB App, which boots Postgres + CloudBeaver + the snapshot cron. Open its URL to see the status page and CloudBeaver under `/admin/`.
6. From your laptop: `python client/domino_db.py tunnel pg-yourname --local-port 5432` → `psql` away.
