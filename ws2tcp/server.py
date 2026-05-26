"""WebSocket ↔ TCP relay for Domino Databases.

Listens on a WebSocket port inside a Domino workspace. The Domino reverse
proxy exposes that port at:

    https://<host>/<owner>/<proj>/notebookSession/<run-id>/proxy/<port>/wire

The matching laptop-side client (`domino-db tunnel`) opens a local TCP
listener (e.g., 127.0.0.1:5432), accepts a connection from `psql` or any
other wire-protocol client, and relays the bytes over a single WSS
connection to this server, which forwards them to the local DB engine
(127.0.0.1:5432 for Postgres, 127.0.0.1:27017 for Mongo).

The relay is byte-transparent — no SQL parsing. The DB engine speaks its
native protocol end-to-end; clients are unmodified.

Auth: the WS Upgrade carries an `Authorization: Bearer <jwt>` header. We
validate against the Domino auth proxy at $DOMINO_API_PROXY/access-token
before opening the TCP socket.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

import httpx
import websockets
from websockets.server import WebSocketServerProtocol

log = logging.getLogger("ws2tcp")

async def validate_bearer(token: str) -> bool:
    """Confirm the bearer was issued by Domino. The local auth proxy will
    echo a token for the calling user; we just need it to not 401."""
    proxy = os.environ.get("DOMINO_API_PROXY", "http://localhost:8899")
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(
                f"{proxy}/access-token",
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code == 200
    except Exception as e:
        log.warning("auth check failed: %s", e)
        return False


async def handle(ws: WebSocketServerProtocol, target_host: str, target_port: int) -> None:
    auth = ws.request_headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not token or not await validate_bearer(token):
        log.info("rejecting connection (bad/missing bearer)")
        await ws.close(code=4401, reason="unauthorized")
        return

    try:
        tcp_reader, tcp_writer = await asyncio.open_connection(target_host, target_port)
    except OSError as e:
        log.error("cannot connect to %s:%s — %s", target_host, target_port, e)
        await ws.close(code=4503, reason="db unavailable")
        return

    log.info("relay open: %s → %s:%s", ws.remote_address, target_host, target_port)

    async def ws_to_tcp() -> None:
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    msg = msg.encode()
                tcp_writer.write(msg)
                await tcp_writer.drain()
        finally:
            tcp_writer.close()

    async def tcp_to_ws() -> None:
        try:
            while True:
                chunk = await tcp_reader.read(65536)
                if not chunk:
                    return
                await ws.send(chunk)
        finally:
            await ws.close()

    await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)
    log.info("relay closed: %s", ws.remote_address)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="0.0.0.0:8765",
                    help="host:port to listen on for WS connections")
    ap.add_argument("--target", default="127.0.0.1:5432",
                    help="host:port of the local DB engine")
    ap.add_argument("--path", default="/wire",
                    help="WS path prefix this server answers on")
    args = ap.parse_args()

    listen_host, listen_port = args.listen.rsplit(":", 1)
    target_host, target_port = args.target.rsplit(":", 1)

    logging.basicConfig(
        level=os.environ.get("DD_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    async def router(ws: WebSocketServerProtocol) -> None:
        if not ws.path.endswith(args.path):
            await ws.close(code=4404, reason="not found")
            return
        await handle(ws, target_host, int(target_port))

    async with websockets.serve(router, listen_host, int(listen_port), max_size=None):
        log.info("ws2tcp listening on %s, forwarding to %s%s",
                 args.listen, args.target, args.path)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
