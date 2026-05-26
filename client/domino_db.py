"""domino-db — laptop-side tunnel client (App-hosted edition).

Opens a local TCP listener (127.0.0.1:5432 for Postgres, 27017 for Mongo)
and tunnels each connection through a WebSocket to a Domino Database App's
`/wire` endpoint. The remote end (dbapp/router.py) relays bytes to the
real engine running internally. Wire protocol is byte-transparent so
`psql`, JDBC, ODBC, and `mongosh` Just Work.

USAGE

    # One-time login (writes ~/.domino-db/config.json)
    python domino_db.py login \\
        --host https://cloud-dogfood.domino.tech \\
        --api-key $DOMINO_USER_API_KEY \\
        --owner nick_goble \\
        --project Database-Extension

    # Tunnel by the App's name (e.g., "pg-myfirst") OR by full URL
    python domino_db.py tunnel pg-myfirst --local-port 5432

    # In another terminal:
    psql "host=127.0.0.1 port=5432 user=domino dbname=postgres"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlparse

import websockets

CONFIG_PATH = Path.home() / ".domino-db" / "config.json"


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    CONFIG_PATH.chmod(0o600)


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------
def cmd_login(args: argparse.Namespace) -> int:
    cfg = {
        "host": args.host.rstrip("/"),
        "api_key": args.api_key,
        "owner": args.owner,
        "project": args.project,
    }
    save_config(cfg)
    print(f"saved credentials to {CONFIG_PATH}")
    return 0


# --------------------------------------------------------------------------
# tunnel
# --------------------------------------------------------------------------
def _build_ws_url(args, cfg) -> str:
    """Convert a user-supplied target into the App's /wire WebSocket URL."""
    target = args.target
    if target.startswith(("http://", "https://", "ws://", "wss://")):
        # Full URL — assume it points at the App; append /wire.
        u = urlparse(target)
        scheme = "wss" if u.scheme in ("https", "wss") else "ws"
        path = u.path.rstrip("/") + "/wire"
        return f"{scheme}://{u.netloc}{path}"

    # Treat target as an App name; build the standard Domino app URL.
    host = cfg.get("host", "").rstrip("/")
    owner = args.owner or cfg.get("owner")
    project = args.project or cfg.get("project")
    if not (host and owner and project):
        raise SystemExit("Need --host/--owner/--project (run `login` first) or pass a full URL.")
    parsed = urlparse(host)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{ws_scheme}://{parsed.netloc}/{owner}/{project}/app/{target}/wire"


async def _relay_one(sock_reader, sock_writer, ws_url: str, token: str) -> None:
    headers = [("X-Domino-Api-Key", token)]
    ssl_ctx = ssl.create_default_context() if ws_url.startswith("wss://") else None
    try:
        async with websockets.connect(
            ws_url, additional_headers=headers, ssl=ssl_ctx,
            max_size=None, ping_interval=20,
        ) as ws:
            print(f"[+] tunnel open ({ws_url})", file=sys.stderr)

            async def sock_to_ws():
                try:
                    while True:
                        chunk = await sock_reader.read(65536)
                        if not chunk: return
                        await ws.send(chunk)
                finally:
                    try: await ws.close()
                    except Exception: pass

            async def ws_to_sock():
                try:
                    async for msg in ws:
                        if isinstance(msg, str): msg = msg.encode()
                        sock_writer.write(msg)
                        await sock_writer.drain()
                finally:
                    sock_writer.close()

            await asyncio.gather(sock_to_ws(), ws_to_sock(), return_exceptions=True)
    except Exception as e:
        print(f"[!] tunnel error: {e}", file=sys.stderr)
        try: sock_writer.close()
        except Exception: pass


async def _serve(args, cfg) -> int:
    token = cfg.get("api_key")
    if not token:
        print("not logged in. run `domino-db login` first.", file=sys.stderr)
        return 2

    ws_url = _build_ws_url(args, cfg)
    server = await asyncio.start_server(
        lambda r, w: asyncio.create_task(_relay_one(r, w, ws_url, token)),
        host="127.0.0.1", port=args.local_port,
    )
    addr = server.sockets[0].getsockname()
    print(f"listening on {addr[0]}:{addr[1]} → {ws_url}", file=sys.stderr)
    async with server:
        await server.serve_forever()
    return 0


def cmd_tunnel(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_serve(args, load_config()))
    except KeyboardInterrupt:
        return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(prog="domino-db")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login", help="store Domino host + API key")
    pl.add_argument("--host", required=True)
    pl.add_argument("--api-key", required=True)
    pl.add_argument("--owner", default=None)
    pl.add_argument("--project", default=None)
    pl.set_defaults(func=cmd_login)

    pt = sub.add_parser("tunnel", help="tunnel a local TCP port to a Domino Database App")
    pt.add_argument("target", help="App name (e.g., pg-myfirst) OR full App URL")
    pt.add_argument("--local-port", type=int, default=5432)
    pt.add_argument("--owner", default=None)
    pt.add_argument("--project", default=None)
    pt.set_defaults(func=cmd_tunnel)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
