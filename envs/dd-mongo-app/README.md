# dd-mongo-env

Mirror of `dd-postgres-env`, but the engine is MongoDB 7. Same JupyterLab-as-IDE shape:

| Port | Service |
|---|---|
| 8888 | JupyterLab (IDE — has jupyter-server-proxy) |
| 8978 | CloudBeaver (reached at `…/proxy/8978/`) |
| 8765 | ws2tcp (reached at `…/proxy/8765/wire`) |
| 27017 | `mongod` (internal only) |

- Cron snapshotter calls `mongodump --oplog --gzip` into the project Dataset
- `preRun.sh` restores from the latest snapshot on cold start

## Required env vars

| Var | Purpose | Default |
|---|---|---|
| `DD_MONGO_PASSWORD` | Admin password | **required** |
| `DD_MONGO_USER` | Admin user | `domino` |
| `DD_MONGO_PORT` | Listen port | `27017` |
| `DD_WS_PORT` | ws2tcp port | `8765` |

## Connecting from your laptop

```bash
domino-db tunnel <run-id>            # opens 127.0.0.1:27017
mongosh "mongodb://domino:<pw>@127.0.0.1:27017/admin"
```
