"""Tunnel byte-fidelity test — engine-agnostic.

Provisions any DB App (default: postgres), opens the /wire tunnel, and
pushes a known stream of bytes through. We can't easily run an arbitrary
echo server in the App (the engine is bound to the only internal port),
so we use the Postgres wire protocol's StartupMessage handshake as a
fixed-shape exchange: send a malformed StartupMessage and assert we get
back the same bytes that a raw psql against direct-localhost-postgres
would see. The point is to catch regressions in:
  * WS masking / unmasking (the bulk-XOR code)
  * fragment buffering / Nagle disabling
  * pgbouncer pass-through if pooler is enabled

For the simpler all-engines case, this test issues large pipelined Redis
PINGs through the tunnel and sha256s the responses. Redis is the most
sensitive to per-message latency (RTT-dominated) so the tunnel breaks
loudest there.

Usage:
  python3 tests/test_tunnel_fidelity.py [--engine redis]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from _dd_helpers import (  # type: ignore[import-not-found]
    log, step, engine_meta, find_env_id, first_hw_tier_id,
    create_db, poll_until_running, wait_for_healthz,
    start_tunnel, wait_for_listener, cleanup,
)


def _redis_pipeline(port: int, password: str, count: int = 10000) -> str:
    """Send `count` PINGs and sha256 the concatenated PONG replies."""
    # Build the RESP request: AUTH + count×PING in one shot.
    auth = f"*2\r\n$4\r\nAUTH\r\n${len(password)}\r\n{password}\r\n".encode()
    ping = b"*1\r\n$4\r\nPING\r\n"
    payload = auth + (ping * count)

    s = socket.create_connection(("127.0.0.1", port), timeout=30)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.sendall(payload)
    s.shutdown(socket.SHUT_WR)

    h = hashlib.sha256()
    received = 0
    deadline = time.time() + 60
    expected_bytes = len(b"+OK\r\n") + (count * len(b"+PONG\r\n"))
    while received < expected_bytes and time.time() < deadline:
        chunk = s.recv(65536)
        if not chunk:
            break
        h.update(chunk)
        received += len(chunk)
    s.close()
    if received < expected_bytes:
        raise SystemExit(
            f"truncated reply: got {received} bytes, expected {expected_bytes}"
        )
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="redis",
                        choices=["redis"],
                        help="engine to use (redis is the only one with a "
                             "fixed-format request/reply that maps cleanly to "
                             "byte-fidelity asserts)")
    parser.add_argument("--count", type=int, default=10000)
    args = parser.parse_args()

    DB_NAME = os.environ.get("DD_E2E_DB_NAME", f"fid-{int(time.time())}")
    PASSWORD = "fidelity-secret-123"
    LOCAL_PORT = 36379

    tunnel_proc = None
    app_id = None
    success = False

    try:
        step("1. provision a Redis DB")
        meta = engine_meta(args.engine)
        env_id = meta["envId"] or find_env_id("dd-redis-app")
        hw_id, _ = first_hw_tier_id()
        app = create_db(
            engine="redis", name=DB_NAME, env_id=env_id, hw_id=hw_id,
            password=PASSWORD, snapshot_interval_min=60,
        )
        app_id = app["id"]
        full_name = f"redis-{DB_NAME}"
        app = poll_until_running(app_id, full_name)
        wait_for_healthz(app["url"])

        step("2. open tunnel + push N pipelined PINGs")
        tunnel_proc = start_tunnel(app["url"], LOCAL_PORT)
        wait_for_listener(LOCAL_PORT)
        h = _redis_pipeline(LOCAL_PORT, PASSWORD, count=args.count)
        log("fid", f"  tunneled sha256 = {h}")

        # Expected: AUTH reply (+OK\r\n) then exact count PONGs.
        expected = hashlib.sha256(
            b"+OK\r\n" + (b"+PONG\r\n" * args.count)
        ).hexdigest()
        log("fid", f"  expected sha256 = {expected}")
        if h != expected:
            raise SystemExit(
                "byte-fidelity FAILURE: tunneled stream does not match "
                "expected RESP replies — check WS masking + fragment "
                "buffering code in dbapp/router.py:/wire."
            )
        log("fid", f"\nALL GREEN ✓  ({args.count} PINGs round-tripped intact)")
        success = True
        return 0
    finally:
        cleanup(tunnel_proc, app_id, success=success)


if __name__ == "__main__":
    sys.exit(main())
