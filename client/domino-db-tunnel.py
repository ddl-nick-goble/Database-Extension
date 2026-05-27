#!/usr/bin/env python3
"""Single-file Postgres-over-WebSocket tunnel for Domino Databases.

ZERO dependencies — only Python stdlib. Works with any python3 that ships
with macOS/Linux. No pip, no venv, no git clone.

USAGE

    # Either pipe straight from GitHub:
    curl -fsSL https://raw.githubusercontent.com/ddl-nick-goble/Database-Extension/main/client/domino-db-tunnel.py \
        | python3 - --url <app-url> --api-key <key> --port 5432

    # Or save it locally and run repeatedly:
    curl -fsSL https://raw.githubusercontent.com/ddl-nick-goble/Database-Extension/main/client/domino-db-tunnel.py \
        -o ~/domino-db-tunnel.py
    python3 ~/domino-db-tunnel.py --url <app-url> --api-key <key> --port 5432

Then in another terminal:
    psql -h 127.0.0.1 -p 5432 -U domino -d postgres
    # or open DBeaver pointed at localhost:5432

Args:
    --url       https://apps.<host>/apps-internal/<appId>/  (no /wire suffix)
    --api-key   Domino API key (or set $DOMINO_API_KEY)
    --port      Local TCP port to expose (default 5432)
"""

from __future__ import annotations

import argparse
import base64
import errno
import os
import secrets
import shutil as _shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time as _time
from urllib.parse import urlparse


# --------------------------------------------------------------------------
# WebSocket framing (RFC 6455, the subset we need)
# --------------------------------------------------------------------------
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


# One SSL context for the lifetime of the process. Building it costs ~30 ms
# (CA bundle load) — doing it per-connection adds noticeable latency at the
# start of every `psql` invocation.
_SSL_CTX = ssl.create_default_context()


def _tune_socket(sock: socket.socket) -> None:
    """Disable Nagle and enable keepalive on a TCP socket. Called on every
    socket that carries Postgres bytes: the laptop-side accepted socket
    AND the outbound socket that ends up wrapped in TLS for the WS hop."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        # SO_KEEPALIVE may be denied in some sandboxes; not fatal.
        pass


def _ws_handshake(sock: socket.socket, host: str, path: str, api_key: str) -> bytes:
    """Send HTTP/1.1 Upgrade, return any leftover bytes that came after the headers."""
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"X-Domino-Api-Key: {api_key}\r\n"
        f"User-Agent: domino-db-tunnel/1.0\r\n"
        f"\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise IOError("server closed connection during WebSocket handshake")
        resp += chunk
    header_end = resp.index(b"\r\n\r\n")
    headers, leftover = resp[:header_end], resp[header_end + 4:]
    status_line = headers.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    if " 101 " not in status_line:
        raise IOError(
            f"WebSocket upgrade rejected: {status_line}\n"
            f"--- response headers ---\n{headers.decode('latin-1', errors='replace')}"
        )
    return leftover


def _mask_xor(mask: bytes, payload: bytes) -> bytes:
    """RFC 6455 client-to-server masking via bulk big-int XOR.

    The naive `bytes(b ^ mask[i & 3] for i, b in enumerate(payload))` runs
    one CPython opcode per byte — at 64 KB per frame it's the dominant
    cost on bulk writes (COPY, large INSERTs). Converting to int via
    `int.from_bytes`, XOR'ing both integers in one operation, and
    converting back is ~10-20× faster on typical payloads because the
    arithmetic happens in libgmp / CPython's bignum C code instead of in
    interpreted bytecode. Still stdlib-only.
    """
    n = len(payload)
    if n == 0:
        return b""
    # Repeat the 4-byte mask to match the payload length
    repeated = (mask * ((n + 3) // 4))[:n]
    return (
        int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")
    ).to_bytes(n, "big")


def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """Send a single masked WS frame (clients MUST mask per RFC 6455)."""
    fin_op = 0x80 | (opcode & 0x0F)
    n = len(payload)
    mask = secrets.token_bytes(4)
    if n < 126:
        header = struct.pack("!BB", fin_op, 0x80 | n)
    elif n < 65536:
        header = struct.pack("!BBH", fin_op, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", fin_op, 0x80 | 127, n)
    sock.sendall(header + mask + _mask_xor(mask, payload))


class _FrameReader:
    """Read WS frames from a stream socket, buffering between calls."""
    def __init__(self, sock: socket.socket, leftover: bytes):
        self.sock = sock
        self.buf = bytearray(leftover)

    def _read(self, n: int) -> bytes:
        out = bytearray()
        if self.buf:
            take = self.buf[:n]
            del self.buf[:n]
            out.extend(take)
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise IOError("WebSocket closed mid-frame")
            out.extend(chunk)
        return bytes(out)

    def next_frame(self) -> tuple[int, bytes, bool]:
        """Return (opcode, payload, fin)."""
        head = self._read(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8))[0]
        mask = self._read(4) if masked else None
        payload = self._read(length) if length else b""
        if masked:
            payload = _mask_xor(mask, payload)
        return opcode, payload, fin


# --------------------------------------------------------------------------
# Per-connection relay
# --------------------------------------------------------------------------
def _relay(local_sock: socket.socket, app_url: str, api_key: str) -> None:
    u = urlparse(app_url)
    if not u.hostname:
        raise ValueError(f"bad app URL: {app_url}")
    host = u.hostname
    is_tls = u.scheme in ("https", "wss", "")
    if u.scheme == "":
        # Default to https if no scheme given
        is_tls = True
    port = u.port or (443 if is_tls else 80)
    path = (u.path.rstrip("/") or "") + "/wire"

    raw = socket.create_connection((host, port), timeout=30)
    _tune_socket(raw)
    if is_tls:
        ws = _SSL_CTX.wrap_socket(raw, server_hostname=host)
    else:
        ws = raw
    try:
        leftover = _ws_handshake(ws, host, path, api_key)
        ws.settimeout(None)
        reader = _FrameReader(ws, leftover)

        stop = threading.Event()
        ws_lock = threading.Lock()  # serialize writes (we masking + pongs)

        def local_to_ws() -> None:
            try:
                while not stop.is_set():
                    chunk = local_sock.recv(65536)
                    if not chunk:
                        return
                    with ws_lock:
                        _send_frame(ws, OP_BIN, chunk)
            except Exception:
                pass
            finally:
                stop.set()

        def ws_to_local() -> None:
            # We're carrying a raw TCP byte stream — there is no message
            # boundary that matters. Flush every fragment as it arrives
            # instead of buffering until FIN. Buffering added pointless
            # latency on small server-pushes (e.g., Postgres's per-row
            # data messages during a result-set stream).
            try:
                while not stop.is_set():
                    opcode, payload, _fin = reader.next_frame()
                    if opcode == OP_CLOSE:
                        return
                    if opcode == OP_PING:
                        with ws_lock:
                            _send_frame(ws, OP_PONG, payload)
                        continue
                    if opcode == OP_PONG:
                        continue
                    if opcode in (OP_BIN, OP_TEXT, OP_CONT) and payload:
                        local_sock.sendall(payload)
            except Exception:
                pass
            finally:
                stop.set()

        t1 = threading.Thread(target=local_to_ws, daemon=True)
        t2 = threading.Thread(target=ws_to_local, daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    finally:
        try: ws.close()
        except Exception: pass
        try: local_sock.close()
        except Exception: pass


# --------------------------------------------------------------------------
# Local TCP listener
# --------------------------------------------------------------------------
def _free_port(port: int) -> None:
    """Kill any process holding 127.0.0.1:<port>. Uses lsof if available
    (macOS + most Linux); falls back to ss/fuser otherwise. Idempotent."""
    pids: set[int] = set()
    if _shutil.which("lsof"):
        try:
            out = subprocess.check_output(
                ["lsof", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
                stderr=subprocess.DEVNULL,
            ).decode()
            pids.update(int(x) for x in out.split() if x.strip().isdigit())
        except subprocess.CalledProcessError:
            pass
    if not pids and _shutil.which("fuser"):
        try:
            out = subprocess.check_output(
                ["fuser", f"{port}/tcp"], stderr=subprocess.DEVNULL,
            ).decode()
            pids.update(int(x) for x in out.split() if x.strip().isdigit())
        except subprocess.CalledProcessError:
            pass
    if not pids:
        return
    print(f"[!] port {port} held by pid(s) {sorted(pids)} — sending SIGTERM",
          file=sys.stderr, flush=True)
    for pid in pids:
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
    _time.sleep(0.8)
    for pid in pids:
        try:
            os.kill(pid, 0)  # still alive?
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _time.sleep(0.3)


def _serve(port: int, app_url: str, api_key: str) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", port))
    except OSError as e:
        if e.errno not in (errno.EADDRINUSE, 48):  # 48 = macOS EADDRINUSE
            raise
        _free_port(port)
        server.close()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
    server.listen(8)
    print(f"listening on 127.0.0.1:{port} → {app_url}/wire", file=sys.stderr, flush=True)
    print(f"  connect with: psql -h 127.0.0.1 -p {port} -U domino -d postgres", file=sys.stderr, flush=True)
    while True:
        client, addr = server.accept()
        _tune_socket(client)
        print(f"[+] accepted {addr[0]}:{addr[1]}", file=sys.stderr, flush=True)
        threading.Thread(
            target=_relay, args=(client, app_url, api_key), daemon=True,
        ).start()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Postgres-over-WebSocket tunnel for Domino Databases (zero deps)",
    )
    p.add_argument("--url", required=True,
                   help="App URL, e.g. https://apps.<host>/apps-internal/<id>/")
    p.add_argument("--api-key", default=os.environ.get("DOMINO_API_KEY"),
                   help="Domino API key (or set $DOMINO_API_KEY)")
    p.add_argument("--port", type=int, default=5432,
                   help="Local TCP port to bind (default 5432)")
    args = p.parse_args()

    if not args.api_key:
        print("error: --api-key required (or set DOMINO_API_KEY)", file=sys.stderr)
        return 2
    try:
        _serve(args.port, args.url.rstrip("/"), args.api_key)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
