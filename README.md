# Domino Databases

A Domino extension that lets users provision real database instances —
**PostgreSQL, MongoDB, MySQL, Redis** — inside their Domino project with
a few clicks. Each database is a long-running **Domino App** that runs
the real engine, plus a WebSocket tunnel so `psql`, `mongosh`, `mysql`,
`redis-cli`, JDBC and ODBC all work unmodified against `localhost`.

```
laptop                              Domino                                          
                                                                                    
psql / mongosh ─► 127.0.0.1:<port>   /<owner>/<proj>/app/<db>/wire                  
mysql / redis-cli                       │                                            
                                        ▼                                            
                                  Flask router :8888  ──►  engine :<port>           
                                        ├─► /admin/   ──►  pgweb / mongo-express    
                                        │                  phpMyAdmin / redis-cmd   
                                        ├─► /         ──►  status page              
                                        └─► /api/     ──►  JSON status              
                                                                                    
                                  /mnt/db/<engine>data                              
                                                                                    
                                  hourly snapshot ─► /domino/datasets/db-<name>/    
```

## Supported engines (v1)

| Engine     | Default port | Admin UI (`/admin/`) | Snapshot tool                | Persistence            |
|------------|:------------:|----------------------|------------------------------|------------------------|
| PostgreSQL | 5432         | pgweb                | `pg_basebackup`              | WAL archiving          |
| MongoDB    | 27017        | mongo-express        | `mongodump --oplog --gzip`   | single-node replSet    |
| MySQL 8.0  | 3306         | phpMyAdmin           | `mysqldump --single-transaction` | InnoDB             |
| Redis 7    | 6379         | redis-commander      | `BGSAVE` + `dump.rdb.gz`     | AOF + RDB              |

All four engines share the same engine-agnostic spine:
- The WS tunnel (`dbapp/router.py:/wire`) is a byte-pump — engine-blind.
- The snapshot mechanism writes to a Domino dataset for versioned restore.
- The wizard UI (engine cards, filter dropdown, stat tiles) is rendered
  entirely from `/api/config.engines` — the catalog the backend resolves
  from the `EngineAdapter` registry at `dbapp/engines/`. Adding an engine
  is a single subclass; no touch-up in the wizard or router needed.
- The wizard's **+ New Database** click streams real-time progress over
  SSE — every Domino API call, the 8 s container-schedule wait broken
  into 2 s heartbeats, retry warnings, and the final `result` payload —
  so the user never sits more than ~2 s without feedback during a
  10–25 s provision cycle.

## Repo layout

| Path | What |
|---|---|
| `app.sh` | Root dispatcher — picks wizard *or* DB-app router from `DD_ROLE`. Prod (`PORT=8888`) runs the wizard under gunicorn; non-`8888` is dev (Flask reloader + kill-stale-on-launch) |
| `app.py` + `static/` + `domino_api.py` | The **wizard** — Flask + static SPA. Provisioning endpoint streams progress as `text/event-stream` |
| `dbapp/app.sh` + `dbapp/router.py` + `dbapp/lifecycle.py` | The **DB App** — boot/admin/snapshot lifecycle + Flask router with `/wire` WS, `/admin/` admin-UI proxy, and `/api/` status |
| `dbapp/engines/{postgres,mongo,mysql,redis}.py` | Per-engine **EngineAdapter** classes; `_common.py` holds shared helpers |
| `envs/dd-{postgres,mongo,mysql,redis}-app/` | Compute environment Dockerfiles (one per engine) |
| `snapshotter/snapshot_<engine>.py` | Per-engine snapshotters (run on a cron loop inside each DB App) |
| `client/` | Laptop tunnel — `domino-db-tunnel.py` (single-file zero-dep) and `domino_db.py` library form |
| `ws2tcp/` | Legacy byte-transparent WS↔TCP relay baked into env images; runs in workspaces as a `nohup` sidecar. The newer `dbapp/router.py:/wire` is the production path |
| `tests/` | `run_all.py` orchestrates `test_engines_unit.py`, `test_env_resolution.py`, per-engine `e2e_<engine>.py`, `test_snapshot_restore.py`, `test_tunnel_fidelity.py` |
| `docs/PRODUCTION_HARDENING.md` | What v1 ships with vs. what banks should expect in v2 |
| `docs/ADDING_AN_ENGINE.md` | 5-step recipe for adding a new engine |
| `docs/PLAN.md` | Original architecture rationale and open risks |

## Quick start

1. Build the four env images in Domino (paste the Dockerfile into the
   "Dockerfile Instructions" field — no FROM line):
   - `envs/dd-postgres-app/Dockerfile`
   - `envs/dd-mongo-app/Dockerfile`
   - `envs/dd-mysql-app/Dockerfile`
   - `envs/dd-redis-app/Dockerfile`

2. (Optional) Set per-engine env IDs on the wizard project. The wizard
   *also* falls back to **Domino env name** lookup — if your env images
   are named exactly `dd-postgres-app` / `dd-mongo-app` / `dd-mysql-app`
   / `dd-redis-app`, no env vars are needed. Set explicit overrides only
   when you've named the images differently:
   ```
   DD_POSTGRES_ENV_ID=<id of dd-postgres-app>
   DD_MONGO_ENV_ID=<id of dd-mongo-app>
   DD_MYSQL_ENV_ID=<id of dd-mysql-app>
   DD_REDIS_ENV_ID=<id of dd-redis-app>
   ```
   `/api/config` surfaces `envIdSource: "envvar" | "byname" | "missing"`
   per engine so the wizard can flag missing images.

3. Run the wizard:
   - **Dev (workspace iteration)**: `PORT=9701 DD_ROLE=wizard bash app.sh`
     — uses Flask's dev server; `app.sh` first kills any stale wizard
     process on `$PORT`.
   - **Prod (deployed Domino App)**: launch as a Domino App on `:8888`;
     `app.sh` runs `gunicorn --workers 2 --threads 4` automatically.

4. In the wizard UI, click **+ New Database** → pick engine → name +
   password → **Provision**. The provisioning log streams every step
   (validate → name-check → config write → create → /start retries with
   2 s heartbeats) and shows the new App's URL on success. The DB App
   boots the engine, the admin UI, and the snapshot cron in parallel.

5. Open the DB App's URL → status page shows the tunnel command + a
   native-client snippet (`psql` / `mongosh` / `mysql` / `redis-cli`).

6. From your laptop — two forms, pick whichever fits your workflow:
   ```bash
   # (a) Single-file, zero-deps, one tunnel by App URL:
   python3 client/domino-db-tunnel.py \
     --url https://apps.<host>/apps-internal/<appId>/ \
     --api-key $DOMINO_API_KEY \
     --port 5432

   # (b) Library form with login + tunnel-by-name (see client/README.md):
   python domino_db.py login --host <host> --api-key $DOMINO_API_KEY --owner <u> --project <p>
   python domino_db.py tunnel pg-myfirst --local-port 5432
   ```
   Then use your native client against `127.0.0.1:<local-port>`:
   ```bash
   psql "host=127.0.0.1 port=5432 user=domino password=<pw> dbname=postgres"
   ```

## Release gate

Before promoting a build to a bank / client:

```bash
python3 tests/run_all.py
# … runs e2e_{postgres,mongo,mysql,redis}.py
# … runs test_snapshot_restore.py (every engine)
# … runs test_tunnel_fidelity.py
# Final summary line MUST be "ALL GREEN ✓"
```

See [docs/PRODUCTION_HARDENING.md](docs/PRODUCTION_HARDENING.md) for the
list of v1-vs-v2 limitations to share with clients before they put real
data behind it.

## Adding a new engine

See [docs/ADDING_AN_ENGINE.md](docs/ADDING_AN_ENGINE.md). The summary:
one EngineAdapter subclass + one snapshotter + one Dockerfile + one
env-var + one test. No changes to the wizard, router, or tunnel are
required — those are all registry-driven.
