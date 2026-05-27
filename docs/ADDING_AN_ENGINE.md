# Adding a new engine

The engine adapter pattern (`dbapp/engines/`) means a new engine is a
**5-touchpoint change**: a Python adapter, a snapshotter, an env image,
a wizard entry-point env var, and a test. The wizard frontend, the
Flask router, the WS tunnel, and the dataset-snapshot mechanism are all
engine-agnostic and need no changes.

## Recipe (worked example: adding ClickHouse)

### 1. `dbapp/engines/clickhouse.py`

Subclass `EngineAdapter`. Required class attributes:

```python
class ClickhouseAdapter(EngineAdapter):
    name         = "clickhouse"
    docs_label   = "ClickHouse"
    description  = "Columnar OLAP via clickhouse-client / JDBC via WS tunnel"
    default_port = 9000          # native TCP, NOT the HTTP port
    app_prefix   = "ch-"
    env_id_var   = "DD_CLICKHOUSE_ENV_ID"
```

Required methods: `restore_or_init`, `start`, `shutdown`,
`health_check`. Optional but recommended: `admin_ui_spec` (return None
to disable `/admin/` for this engine), `connection_strings` (list of
`ConnectionSnippet`), `snapshot_env`.

Register at module bottom:

```python
_register(ClickhouseAdapter())
```

The registry auto-loads `dbapp/engines/<name>.py` on import — drop the
file in place and `engines.all_engines()` picks it up.

### 2. `snapshotter/snapshot_clickhouse.py`

Mirror the shape of `snapshot_postgres.py`. The adapter's
`snapshot_script_name()` defaults to `snapshot_<engine>.py`. Required
env reads: `DD_DB_ID`, `DD_SNAPSHOT_DIR`, plus whatever you put in
`adapter.snapshot_env()`. Layout convention: `snapshots/<ts>/<dumpfile>`
with a `snapshots/latest` symlink to the most recent.

### 3. `envs/dd-clickhouse-app/Dockerfile`

Domino compute environment, base = DSE. Single-line RUNs (Domino UI
strips `\` continuations across pipes). Mandatory ENV:

```
ENV DD_ROLE=clickhouse DD_ENGINE=clickhouse DD_OPT_DIR=/opt/dd
```

Bake `/opt/dd/dbapp`, `/opt/dd/snapshotter`, `/opt/dd/app.sh` so the env
works in any project (see the `dd-postgres-app/Dockerfile` line that
curls from main).

### 4. Wizard env-var

Set `DD_CLICKHOUSE_ENV_ID=<env id>` in the wizard project's environment
variables (the `env_id_var` you declared on the adapter). The wizard
will pre-select that env when a user picks ClickHouse in step 2 of the
wizard.

### 5. `tests/e2e_clickhouse.py`

Clone `tests/e2e_redis.py` — it's the shortest. Swap the native client
calls for `clickhouse-client --host 127.0.0.1 --port <tunnel-port>`.
Add the engine to the per-engine list in `tests/run_all.py` and
`tests/test_snapshot_restore.py:PROBES`.

## What you do NOT need to touch

- `dbapp/lifecycle.py` — `boot()` is engine-agnostic, the adapter does
  the work.
- `dbapp/router.py` — `/wire`, `/admin/`, `/healthz`, and the status
  page all read from the adapter.
- `static/index.html` + `static/app.js` — engine cards / filters / stats
  are rendered from `/api/config.engines`, which is built from the
  registry.
- `app.py` (wizard backend) — engine list, prefix, port, env-id are all
  registry-driven.
- The tunnel client (`client/domino-db-tunnel.py`) — byte-transparent;
  doesn't know what protocol is flowing through.
- The snapshot orchestrator (`lifecycle.schedule_snapshotter`) —
  engine-agnostic; reads script name + env from the adapter.

## Sanity check

Before opening a PR:

```bash
python3 tests/test_engines_unit.py
# OK — 5 engines registered cleanly
```

…and then the real engine e2e through the wizard.
