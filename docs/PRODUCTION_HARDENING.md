# Production Hardening — Known Limitations & v2 Roadmap

This is what v1 ships with and where banks / regulated clients should
understand the gaps before they put real data behind it.

v1 is a **playground**. It's safe enough for evaluation, demos, and
non-sensitive internal data. It is **not** a multi-tenant DBaaS yet — the
gaps below are the v2 work.

## What v1 does well

- **Network isolation** — every DB App is bound to 127.0.0.1 inside its
  own pod. The only externally-reachable port is `:8888` (the Flask
  router), and that is itself behind Domino's authenticated app proxy.
- **Tunnel auth** — the `/wire` WebSocket is gated by Domino's app-level
  auth. A laptop client must present a Domino API key tied to a user
  that has access to the project the DB App lives in.
- **At-rest data** — the Postgres / MySQL / Mongo / Redis data dir lives
  on Domino's volume layer (encrypted by whatever Domino + cloud provider
  is configured for). Dataset snapshots inherit the same encryption.
- **Snapshots** — every engine writes versioned snapshots to a Domino
  dataset on the schedule the user picked at create time. The snapshotter
  also runs at SIGTERM so a clean App stop captures the last write.
- **Crash recovery** — Redis runs AOF (`appendfsync everysec`), Postgres
  runs WAL archiving, MySQL is InnoDB-default, Mongo runs a single-node
  replSet with the oplog. All four survive `kill -9 mysqld` and equivalents.
- **Resource caps** — each engine ships with a sensible memory bound
  (Redis `maxmemory 1gb`, MySQL `innodb_buffer_pool_size=512M`, Mongo
  `wiredTigerCacheSizeGB=0.5`) so a runaway DB doesn't OOM-kill the App.
- **Connection pooling** — Postgres ships with PgBouncer in front (501
  client conns → 25 backend conns, transaction mode).
- **Graceful shutdown** — `adapter.shutdown()` runs on SIGTERM and gives
  each engine its native clean-stop (`pg_ctl stop`, `mongosh
  shutdownServer`, `mysqladmin shutdown`, `redis-cli SHUTDOWN SAVE`).
- **Health probing** — `/healthz` calls the adapter's engine-specific
  health check. Domino's app-monitor surfaces sick Apps in the dashboard.

## What v1 does NOT do (v2 work)

### Secrets

- DB passwords live in **plaintext JSON on a Domino dataset**
  (`<dataset>/_dd_configs/<app-name>.json`). The file is mode 0600, but
  it is readable by anyone in the project with file access. **Do not put
  a real production credential here.**
- Passwords are passed to engine processes via env vars and CLI flags —
  visible in `/proc/<pid>/environ` to anything in the same pod.
- **v2:** Domino secret store integration (read at boot, never persist
  to dataset; rotate without re-create).

### TLS

- The laptop ↔ App connection is HTTPS (Domino's proxy). The App ↔ engine
  connection is plain TCP on localhost. No TLS termination inside the pod.
- **v2:** If the user terminates the tunnel inside a Domino workspace
  rather than at their laptop, the tunneled traffic crosses Domino's
  internal network in clear. Add stunnel/tlswrapper inside the tunnel
  client for cross-pod use cases.

### Audit / access logs

- No per-connection audit log of who connected through `/wire`. The
  Flask access log exists but has no Domino user identity attached.
- **v2:** thread the Domino auth claims through `/wire` and emit a
  structured audit event per session open/close.

### Per-database network isolation

- Two DBs in the same project share the same network namespace
  (Domino-compute default). If one Postgres App is compromised, it can
  reach the other Postgres App's `:5432` directly. (In practice it can't
  authenticate — they have separate passwords — but the network path
  exists.)
- **v2:** NetworkPolicy / pod-level isolation. Requires Domino-platform
  work, not in scope here.

### Backups beyond v1

- Postgres uses `pg_basebackup` (good).
- MySQL uses `mysqldump --single-transaction` — logical, slow on big DBs.
  **v1.1:** swap to XtraBackup for binary-format hot backups.
- Mongo uses `mongodump --oplog` — requires a primary (we run single-node
  replSet). Restore via `mongorestore --oplogReplay`.
- Redis uses `BGSAVE` + dataset versioning of `dump.rdb`. Combined with
  AOF for crash recovery, this covers most failure modes.
- **None of the above are PITR.** A user can restore to "the snapshot
  taken at <interval> minutes ago", not "to 2026-05-27 14:32:11".
- **v2:** wire WAL-archiving (Postgres) / binlog (MySQL) into the
  snapshotter so PITR is possible.

### Multi-tenancy

- v1 assumes one Domino project per group of databases. Cross-project
  database sharing works through Domino dataset mounts but isn't
  surfaced in the wizard UI.
- **v2:** "shared databases" listing + project ACLs on individual DBs.

### Resource enforcement

- Each engine has soft memory caps (above). There is **no** CPU quota or
  disk-quota enforcement beyond what Domino's hardware tier provides. A
  noisy-neighbor DB can starve a sibling in the same project's tier.
- **v2:** cgroup-driven limits via Domino's pod-resource API.

## Operational notes

- **Lost laptop:** the laptop's `~/.domino-db/config.json` carries the
  Domino API key (mode 0600). On theft, the user must rotate that key in
  Domino's Account Settings; existing tunnels stop working immediately
  because the proxy re-validates per WS handshake.
- **Forgotten password:** there is no UI password reset in v1. Stop the
  DB App, edit the JSON on the dataset (`engine.password` field), restart
  the App. The engine adapter will reuse the stored password as the
  bootstrap creds on a fresh init, but on a warm restart it uses whatever
  the engine already has stored — so you must `ALTER USER … PASSWORD …`
  (or the engine equivalent) before flipping the JSON.
- **Lost engine container:** the snapshotter + dataset versioning are
  designed for exactly this. Recreate the DB App with the same name
  (`pg-foo` → `pg-foo`) and the lifecycle picks up the latest snapshot
  on cold boot. This is what `test_snapshot_restore.py` exercises.

## Test gate before each release

Run `tests/run_all.py` from a wizard workspace. It must report **ALL
GREEN** before promoting a build to a bank/client. The gate includes:

- `e2e_postgres.py`, `e2e_mongo.py`, `e2e_mysql.py`, `e2e_redis.py`
- `test_snapshot_restore.py` — every engine survives App-delete + restore
- `test_tunnel_fidelity.py` — 10k pipelined Redis PINGs round-trip
  through `/wire` with zero corruption

`tests/test_engines_unit.py` is the no-network sanity check that runs in
~1 second; CI should run it on every commit.
