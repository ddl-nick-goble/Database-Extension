# ws2tcp

Byte-transparent WebSocket ↔ TCP relay. Runs inside a Domino "database workspace" alongside the real engine (Postgres, MongoDB, …) and exposes its wire protocol via Domino's HTTPS-only reverse proxy.

## Run locally (for development)

```bash
pip install -r requirements.txt
python server.py --listen 0.0.0.0:8765 --target 127.0.0.1:5432
```

Set `DOMINO_API_PROXY=http://localhost:8899` (already present inside any Domino execution) so the bearer check works. Outside Domino, the bearer check will reject everything — short-circuit it with an `--insecure-no-auth` flag if/when needed for offline tests.

## Inside the env image

It's installed at `/opt/dd/ws2tcp_server.py` and started by `preRun.sh` as a backgrounded `nohup` process. The Domino proxy then routes WS upgrades at `/.../notebookSession/<id>/proxy/8765/wire` straight to it.

## Why a relay (and not PostgREST etc.)

We want `psql`, JDBC, ODBC, and `mongosh` to **just work** unmodified. A relay carries the full native wire protocol so clients don't know they're tunneled. PostgREST/RESTHeart are good for HTTP-native callers — those run as separate sidecars on different ports, not through this relay.
