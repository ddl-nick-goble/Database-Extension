# domino-db (laptop client)

Tunnels a local TCP port (`127.0.0.1:5432`, `127.0.0.1:27017`, …) to a Domino **Database App** so `psql`, JDBC, ODBC, `mongosh`, DBeaver, etc. Just Work — they don't know they're tunneled.

Spike implementation: Python. v1 ships as a single static Go binary; the wire format is identical so the SDK doesn't need to change.

## Install

```bash
pip install -r requirements.txt
```

## Use

```bash
# One-time login
python domino_db.py login \
    --host https://cloud-dogfood.domino.tech \
    --api-key $DOMINO_USER_API_KEY \
    --owner nick_goble \
    --project Database-Extension

# Open a tunnel by app name (the wizard names DB apps `pg-<name>` or `mongo-<name>`)
python domino_db.py tunnel pg-myfirst --local-port 5432

# In another terminal:
psql "host=127.0.0.1 port=5432 user=domino password=<DD_PG_PASSWORD> dbname=postgres"
```

You can also pass a full App URL (handy if your wizard shows the URL):

```bash
python domino_db.py tunnel https://cloud-dogfood.domino.tech/nick_goble/Database-Extension/app/pg-myfirst/ \
    --local-port 5432
```

## TODO before v1

- Single static Go binary for distribution
- Reconnect-with-backoff when the WS drops mid-session
- Short-lived JWT (rather than long-lived API key) — wizard issues it
