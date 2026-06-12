#!/usr/bin/env python3
"""Live end-to-end check: workspace -> WS tunnel -> Domino app proxy ->
DB-app pod -> Postgres. Runs the tunnel listener in a daemon thread inside
this same process (so it needs no persistent background process), then talks
real Postgres wire protocol through it with pg8000.
"""
import importlib.util
import os
import sys
import threading
import time

import pg8000.dbapi

APP_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ["DBAPP_URL"]
API_KEY = os.environ["DOMINO_USER_API_KEY"]
LOCAL_PORT = int(os.environ.get("DBAPP_LOCAL_PORT", "5432"))
PASSWORD = os.environ.get("DBAPP_PW", "DominoDemo2026!")

# Load the single-file tunnel client (filename has hyphens -> importlib).
spec = importlib.util.spec_from_file_location(
    "domino_db_tunnel", "/mnt/code/client/domino-db-tunnel.py"
)
tun = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tun)

# Start the local TCP listener -> WS relay in a daemon thread.
t = threading.Thread(
    target=tun._serve, args=(LOCAL_PORT, APP_URL.rstrip("/"), API_KEY), daemon=True
)
t.start()

# Wait for the listener to bind.
import socket
for _ in range(50):
    s = socket.socket()
    if s.connect_ex(("127.0.0.1", LOCAL_PORT)) == 0:
        s.close()
        break
    s.close()
    time.sleep(0.1)
else:
    print("FAIL: tunnel never bound", flush=True)
    sys.exit(1)

print(f"tunnel up on 127.0.0.1:{LOCAL_PORT}; connecting psql-equivalent...", flush=True)

conn = pg8000.dbapi.connect(
    host="127.0.0.1", port=LOCAL_PORT, user="domino",
    password=PASSWORD, database="postgres",
)
cur = conn.cursor()

cur.execute("SELECT version();")
print("server version:", cur.fetchone()[0], flush=True)

cur.execute("CREATE TABLE IF NOT EXISTS demo_dbapp (id serial primary key, note text, at timestamptz default now());")
cur.execute("INSERT INTO demo_dbapp (note) VALUES (%s) RETURNING id;", ("hello from the Domino workspace via TCP-over-WebSocket",))
new_id = cur.fetchone()[0]
conn.commit()
print("inserted row id:", new_id, flush=True)

cur.execute("SELECT id, note, at FROM demo_dbapp ORDER BY id;")
rows = cur.fetchall()
print(f"rows in demo_dbapp: {len(rows)}", flush=True)
for r in rows:
    print("  ", r, flush=True)

cur.execute("SELECT current_database(), current_user, inet_server_addr(), inet_server_port();")
print("session:", cur.fetchone(), flush=True)

cur.close()
conn.close()
print("VERIFY OK ✓", flush=True)
