#!/usr/bin/env python3
import hmac
import json
import logging
import os
import threading
import time
import uuid

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import daemon_client


HOST = os.getenv("CODEX_WATCH_HOST", "127.0.0.1")
PORT = int(os.getenv("CODEX_WATCH_PORT", "8765"))
TOKEN = os.getenv("CODEX_WATCH_TOKEN", "")
VERSION = "0.4.0"
MAX_BODY_SIZE = 1024 * 1024
MAX_NOTIFICATIONS = 50
NOTIFY_PUSH_DELAY = int(os.getenv("CODEX_WATCH_NOTIFY_DELAY", "60"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("codex-watch")

STATE_LOCK = threading.RLock()
notifications = {}
pending_completions = {}


def queue_notification(item):
    event_id = str(item.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("event_id required")
    with STATE_LOCK:
        if event_id in notifications:
            return False
        notifications[event_id] = item
        while len(notifications) > MAX_NOTIFICATIONS:
            notifications.pop(next(iter(notifications)))
    log.info("notification queued: %s", event_id)
    return True


def cancel_approval_notification(approval_id):
    event_id = "appr-" + str(approval_id)
    with STATE_LOCK:
        return notifications.pop(event_id, None) is not None


def cancel_completion(thread_id):
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return
    with STATE_LOCK:
        pending = pending_completions.pop(thread_id, None)
        if pending:
            pending["timer"].cancel()
        stale = [
            event_id for event_id, item in notifications.items()
            if item.get("type") == "task_completed"
            and (item.get("payload") or {}).get("thread_id") == thread_id
        ]
        for event_id in stale:
            notifications.pop(event_id, None)
    if pending or stale:
        log.info("completion notification cancelled: %s", thread_id)


def _publish_completion(thread_id, turn_id):
    with STATE_LOCK:
        pending = pending_completions.get(thread_id)
        if not pending or pending["turn_id"] != turn_id:
            return
        pending_completions.pop(thread_id, None)
        queue_notification(pending["notification"])


def schedule_completion(data):
    thread_id = str(data.get("thread_id") or "").strip()
    turn_id = str(data.get("turn_id") or "").strip()
    cwd = str(data.get("cwd") or "").strip()
    message = str(data.get("message") or "Codex turn completed.").strip()
    if not thread_id or len(thread_id) > 256:
        raise ValueError("valid thread_id required")
    if not turn_id or len(turn_id) > 256:
        raise ValueError("valid turn_id required")
    if len(message) > 16384:
        raise ValueError("message too large")
    project = os.path.basename(cwd.rstrip("/")) if cwd else "codex"
    event_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        "codex-watch:%s:%s" % (thread_id, turn_id)
    ).hex
    item = {
        "event_id": event_id,
        "type": "task_completed",
        "title": "Codex · " + project,
        "body": message,
        "payload": {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "cwd": cwd,
        },
        "created_at": int(time.time()),
    }
    with STATE_LOCK:
        if event_id in notifications:
            return {"ok": True, "event_id": event_id, "delay": NOTIFY_PUSH_DELAY, "queued": True}
        previous = pending_completions.get(thread_id)
        if previous and previous["turn_id"] == turn_id:
            if message and message != "Codex turn completed.":
                previous["notification"]["body"] = message
            return {"ok": True, "event_id": event_id, "delay": NOTIFY_PUSH_DELAY, "duplicate": True}
        if previous:
            previous["timer"].cancel()
        stale = [
            queued_id for queued_id, queued in notifications.items()
            if queued.get("type") == "task_completed"
            and (queued.get("payload") or {}).get("thread_id") == thread_id
        ]
        for queued_id in stale:
            notifications.pop(queued_id, None)
        timer = threading.Timer(
            NOTIFY_PUSH_DELAY,
            _publish_completion,
            args=(thread_id, turn_id),
        )
        timer.daemon = True
        pending_completions[thread_id] = {
            "turn_id": turn_id,
            "timer": timer,
            "notification": item,
        }
        timer.start()
    log.info(
        "completion scheduled thread=%s turn=%s delay=%ss",
        thread_id, turn_id, NOTIFY_PUSH_DELAY
    )
    return {"ok": True, "event_id": event_id, "delay": NOTIFY_PUSH_DELAY}


def notification_list(limit):
    with STATE_LOCK:
        return list(notifications.values())[:limit]


def acknowledge_notification(event_id):
    with STATE_LOCK:
        return notifications.pop(event_id, None) is not None


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexWatchBridge/" + VERSION

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)

    def send_json(self, status, data):
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def require_auth(self):
        expected = "Bearer " + TOKEN
        actual = self.headers.get("Authorization", "")
        if hmac.compare_digest(actual, expected):
            return True
        self.send_json(401, {"ok": False, "error": "unauthorized"})
        return False

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid content length")
        if length <= 0 or length > MAX_BODY_SIZE:
            raise ValueError("invalid body size")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid JSON")
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(200, {
                "ok": True,
                "service": "codex-watch-bridge",
                "version": VERSION,
            })
            return
        if not self.require_auth():
            return
        if parsed.path == "/v1/health":
            client = daemon_client.INSTANCE
            self.send_json(200, {
                "ok": True,
                "service": "codex-watch-bridge",
                "version": VERSION,
                "daemon_connected": bool(client and client.connected),
                "notification_count": len(notification_list(MAX_NOTIFICATIONS)),
                "timestamp": int(time.time()),
            })
            return
        if parsed.path == "/v1/notifications":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            items = notification_list(max(1, min(limit, MAX_NOTIFICATIONS)))
            self.send_json(200, {"ok": True, "count": len(items), "notifications": items})
            return
        if parsed.path == "/v1/approvals":
            params = parse_qs(parsed.query)
            status = params.get("status", ["pending"])[0]
            if status not in ("pending", "sent", "all"):
                self.send_json(400, {"ok": False, "error": "invalid status"})
                return
            approvals = daemon_client.INSTANCE.list_approvals(status) if daemon_client.INSTANCE else []
            self.send_json(200, {"ok": True, "count": len(approvals), "approvals": approvals})
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self.require_auth():
            return
        try:
            data = self.read_json()
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
            return
        path = urlparse(self.path).path
        try:
            if path == "/v1/notifications/ack":
                event_id = str(data.get("event_id") or "").strip()
                if not event_id:
                    raise ValueError("event_id required")
                self.send_json(200, {
                    "ok": True,
                    "removed": acknowledge_notification(event_id),
                })
                return
            if path == "/v1/threads/reply":
                thread_id = str(data.get("thread_id") or "").strip()
                text = str(data.get("text") or "").strip()
                if not thread_id or not text:
                    raise ValueError("thread_id and text required")
                if len(text) > 16000:
                    raise ValueError("text too large")
                if not daemon_client.INSTANCE:
                    self.send_json(503, {"ok": False, "error": "daemon client unavailable"})
                    return
                result = daemon_client.INSTANCE.send_turn(thread_id, text)
                self.send_json(200 if result.get("ok") else 409, result)
                return
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["v1", "approvals"] and parts[3] == "decision":
                approval_id = parts[2]
                decision = str(data.get("decision") or "").lower()
                if not daemon_client.INSTANCE:
                    self.send_json(503, {"ok": False, "error": "daemon client unavailable"})
                    return
                result = daemon_client.INSTANCE.handle_decision(approval_id, decision)
                self.send_json(200 if result.get("ok") else 409, result)
                return
            self.send_json(404, {"ok": False, "error": "not found"})
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            log.exception("request failed")
            self.send_json(500, {"ok": False, "error": str(exc)})


def main():
    if len(TOKEN) < 32:
        raise SystemExit("CODEX_WATCH_TOKEN must be at least 32 characters")
    daemon_client.INSTANCE = daemon_client.DaemonClient(
        notification_sink=queue_notification,
        turn_started_sink=cancel_completion,
        turn_completed_sink=schedule_completion,
        notification_cancel_sink=cancel_approval_notification,
    )
    daemon_client.INSTANCE.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    log.info("Bridge listening on %s:%s", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        daemon_client.INSTANCE.stop()


if __name__ == "__main__":
    main()
