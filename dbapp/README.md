# dbapp/ — the Flask router that each Domino Databases App runs

When a Domino App provisioned by the wizard starts up, `/mnt/code/app.sh` sees `DD_ROLE=postgres` (or `mongo`) — set by the compute environment image — and execs `dbapp/app.sh`.

That script:

1. Reads the per-DB config from `/mnt/code/dbapps/<app-name>.json` (the wizard writes this file before creating the app).
2. Calls `lifecycle.boot()` which restores `/mnt/db/<engine>data` from the latest Domino Dataset snapshot (or initializes a fresh cluster), starts Postgres/MongoDB on `127.0.0.1`, starts CloudBeaver on `127.0.0.1:8978`, and schedules the snapshot cron.
3. Launches `router.py` on `$PORT` (8888 in prod). The router is the only thing bound externally.

## What the router exposes on port 8888

| Path | Handler | Purpose |
|---|---|---|
| `GET  /` | status page (HTML) | "Your database is running" — link to admin UI, sample connection commands |
| `GET  /api/status` | JSON | Engine, internal port, etc. |
| `GET  /api/config` | JSON (sanitized) | DB metadata without the password |
| `WS   /wire` | byte-transparent relay | The laptop `domino-db` CLI tunnels native wire protocol through this |
| `*    /admin/*` | reverse-proxy to CloudBeaver | Web SQL IDE, pre-connected to the local DB |
| `GET  /healthz` | health probe | Returns 200 if the engine is reachable on its internal port |

## Why multiplex through one port

Domino Apps expose exactly one HTTP port. We need:
- An admin UI (CloudBeaver)
- A wire-protocol tunnel for unmodified clients (psql, mongosh, JDBC, ODBC)
- A status page

So everything goes through the Flask router. The `/admin/` reverse proxy keeps CloudBeaver internal and hidden behind Domino's auth. The `/wire` WebSocket is the production-grade pattern (same as Neon's serverless driver).

## Local development

```bash
echo '{"engine":"postgres","db_id":"local","password":"x","port":5432}' > /tmp/dd-config.json
cd /mnt/code
DD_CONFIG=/tmp/dd-config.json PORT=8501 DD_ROLE=postgres bash app.sh
```

(You'll need Postgres installed locally for full boot to succeed; otherwise just import `dbapp.router` and exercise individual routes.)
