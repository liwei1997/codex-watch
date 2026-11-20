#!/usr/bin/env python3
"""
Minimal WebSocket-over-unix-socket JSON-RPC client for the codex
app-server daemon.

Ported from the 2026-11-16 approval spike (verified against codex 0.159.3).
Protocol facts:
  - One complete JSON document per WebSocket frame (no line terminators).
  - Client->server frames must be masked.
  - Server->client frames do NOT carry a "jsonrpc" field.
  - Server requests ("id" + "method") are answered with {"id", "result"}.
"""
import base64
import json
import logging
import os
import select
import socket
import struct
import threading
import time
import uuid

log = logging.getLogger("codex-watch.ws")

DAEMON_SOCK = os.environ.get(
    "CODEX_DAEMON_SOCK",
    "/home/liwei/.codex/app-server-control/app-server-control.sock",
)

# Notifications worth keeping at INFO level in a long-lived daemon;
# everything else is demoted to debug to keep the log usable.
INTERESTING_NOTIF = frozenset({
    "thread/status/changed",
    "thread/started",
    "thread/deleted",
    "serverRequest/resolved",
    "turn/completed",
})

# WebSocket liveness: ping every 30s; disconnect if no data for 90s
PING_INTERVAL = 30
LIVENESS_TIMEOUT = 300


def _ws_handshake(s):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET / HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: websocket\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "\r\n"
    )
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            raise RuntimeError("handshake: connection closed")
        buf += d
    head = buf.split(b"\r\n\r\n", 1)[0].decode()
    if "101" not in head.split("\r\n", 1)[0]:
        raise RuntimeError(f"handshake failed: {head}")
    return buf.split(b"\r\n\r\n", 1)[1]  # any leftover payload


def _send_frame(s, payload: bytes, opcode=0x1):
    header = bytearray([0x80 | opcode])
    n = len(payload)
    mask = os.urandom(4)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    s.sendall(bytes(header) + masked)


def _recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        d = s.recv(n - len(buf))
        if not d:
            raise RuntimeError("connection closed")
        buf += d
    return buf


class WSClient:
    """One WebSocket connection to the daemon control socket.

    A reader thread parses incoming frames and dispatches:
      - server requests  ("id" + "method")  -> on_server_request(method, params, msg)
      - notifications    ("method" only)    -> on_notification(method, params, msg)
      - request replies  ("id" only)        -> resolves pending request() futures

    The connection is single-use: when it closes the on_close callback fires
    and the owner is expected to create a fresh client (reconnect).
    """

    def __init__(
        self,
        name,
        sock_path=DAEMON_SOCK,
        on_notification=None,
        on_server_request=None,
        on_close=None,
        id_offset=1_000_000_000,
    ):
        self.name = name
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self.on_close = on_close
        # Client request ids start far above the daemon's server-request id
        # space (small per-thread sequences) to avoid collisions.
        self.next_id = id_offset + int(time.time()) % 100_000
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect(sock_path)
        _ws_handshake(self.sock)
        self.buf = b""
        self.pending = {}
        self.lock = threading.Lock()
        self.closed = False
        self.reader = threading.Thread(
            target=self._reader, daemon=True, name="ws-" + name
        )
        self.reader.start()

    def log(self, tag, obj, level="info"):
        line = f"{self.name} {tag} {json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj}"
        line = line[:2000]
        if level == "debug":
            log.debug(line)
        else:
            log.info(line)

    def send(self, msg):
        with self.lock:
            _send_frame(self.sock, json.dumps(msg).encode())

    def notify(self, method, params=None):
        m = {"method": method}
        if params is not None:
            m["params"] = params
        self.send(m)

    def request(self, method, params=None, timeout=30):
        with self.lock:
            rid = self.next_id
            self.next_id += 1
        fut = threading.Event()
        box = {}
        self.pending[rid] = (fut, box)
        m = {"id": rid, "method": method}
        if params is not None:
            m["params"] = params
        try:
            self.send(m)
        except Exception:
            self.pending.pop(rid, None)
            raise
        if not fut.wait(timeout):
            self.pending.pop(rid, None)
            raise TimeoutError(method)
        if "error" in box:
            raise RuntimeError(f"{method}: {json.dumps(box['error'])}")
        return box.get("result")

    def respond(self, rid, result):
        """Answer a server request with its original id (fire-and-forget).

        Resolution is observed via the daemon's serverRequest/resolved
        notification, not via the response to this frame.
        """
        self.send({"id": rid, "result": result})

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass

    def _reader(self):
        last_data = time.time()
        last_ping = time.time()
        while not self.closed:
            try:
                r, _, _ = select.select([self.sock], [], [], 0.5)
                if r:
                    b0, b1 = _recv_exact(self.sock, 2)
                    last_data = time.time()
                else:
                    # No data: check liveness timeout
                    if time.time() - last_data > LIVENESS_TIMEOUT:
                        log.warning("%s: no data for %ss, forcing reconnect",
                                    self.name, LIVENESS_TIMEOUT)
                        break
                    # Periodic ping to keep connection alive
                    if time.time() - last_ping > PING_INTERVAL:
                        try:
                            with self.lock:
                                _send_frame(self.sock, b"", opcode=0x9)
                            last_ping = time.time()
                        except OSError:
                            break
                    continue
            except (socket.timeout, RuntimeError, OSError):
                break
            except Exception as e:
                log.warning("%s reader error: %r", self.name, e)
                break
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            n = b1 & 0x7F
            try:
                if n == 126:
                    n = struct.unpack(">H", _recv_exact(self.sock, 2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", _recv_exact(self.sock, 8))[0]
                mask = _recv_exact(self.sock, 4) if masked else None
                payload = _recv_exact(self.sock, n) if n else b""
            except (RuntimeError, OSError):
                break
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                self.log("WS-CLOSE", payload[:100])
                break
            if opcode == 0x9:  # ping -> pong
                try:
                    with self.lock:
                        _send_frame(self.sock, payload, opcode=0xA)
                except OSError:
                    break
                continue
            if opcode == 0xA:  # pong (just a liveness signal)
                continue
            if opcode in (0x0, 0x1, 0x2):  # continuation / text / binary
                self.buf += payload
                try:
                    self._drain()
                except Exception as e:
                    log.warning("%s drain error: %r", self.name, e)
        self._finish()

    def _finish(self):
        if self.closed:
            return
        self.closed = True
        # Fail any outstanding request() waits.
        for rid, (fut, box) in list(self.pending.items()):
            box["error"] = {"code": -1, "message": "connection closed"}
            fut.set()
        if self.on_close:
            try:
                self.on_close()
            except Exception as e:
                log.warning("%s on_close error: %r", self.name, e)

    def _drain(self):
        """Each WS frame carries one or more complete JSON documents (no line
        terminators). Parse greedily with raw_decode; fall back to NDJSON
        line splitting for frames that do contain newlines."""
        dec = json.JSONDecoder()
        text = self.buf.decode("utf-8", "replace")
        self.buf = b""
        while True:
            text = text.lstrip()
            if not text:
                return
            try:
                obj, end = dec.raw_decode(text)
            except Exception:
                if "\n" in text:
                    line, text = text.split("\n", 1)
                    line = line.strip()
                    if line:
                        try:
                            self.handle(json.loads(line))
                        except Exception:
                            self.log("BADFRAME", line[:300])
                    continue
                self.buf = text.encode("utf-8")  # incomplete; wait for more
                return
            self.handle(obj)
            text = text[end:]

    def handle(self, msg):
        if "method" in msg and "id" in msg:
            self.log("REQ-IN", msg)
            if self.on_server_request:
                try:
                    self.on_server_request(msg["method"], msg.get("params"), msg)
                except Exception as e:
                    log.warning("%s server-request handler error: %r", self.name, e)
        elif "method" in msg:
            lvl = "info" if msg.get("method") in INTERESTING_NOTIF else "debug"
            self.log("NOTIF", msg, level=lvl)
            if self.on_notification:
                try:
                    self.on_notification(msg["method"], msg.get("params"), msg)
                except Exception as e:
                    log.warning("%s notification handler error: %r", self.name, e)
        elif "id" in msg:
            item = self.pending.pop(msg["id"], None)
            if item:
                fut, box = item
                box.update(msg)
                fut.set()
            else:
                self.log("RESP-ORPHAN", msg)
