#!/usr/bin/env python3
import json
import logging
import os
import socket
import threading
import time
import uuid

from wsclient import WSClient


log = logging.getLogger("codex-watch.daemon")
DAEMON_SOCK = os.environ.get(
    "CODEX_DAEMON_SOCK",
    "/home/liwei/.codex/app-server-control/app-server-control.sock",
)
ALL_SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "subAgent", "unknown"]
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "execCommandApproval",
    "item/fileChange/requestApproval",
    "applyPatchApproval",
    "item/permissions/requestApproval",
}
VALID_DECISIONS = ("accept", "decline", "cancel")
APPROVAL_PUSH_DELAY = int(os.getenv("CODEX_WATCH_NOTIFY_DELAY", "60"))


class DaemonClient:
    def __init__(
        self,
        notification_sink=None,
        turn_started_sink=None,
        notification_cancel_sink=None,
        sock_path=DAEMON_SOCK,
    ):
        self.notification_sink = notification_sink
        self.turn_started_sink = turn_started_sink
        self.notification_cancel_sink = notification_cancel_sink
        self.sock_path = sock_path
        self.client = None
        self.connected = False
        self.joined = set()
        self.threads = {}
        self.approvals = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.closed = threading.Event()
        self.thread = None
        self.joining = set()

    def start(self):
        self.thread = threading.Thread(target=self._run_forever, name="codex-daemon-client", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.client:
            self.client.close()

    def _run_forever(self):
        delay = 2
        while not self.stop_event.is_set():
            self.closed.clear()
            try:
                self._connect_and_serve()
                delay = 2
            except Exception as exc:
                log.warning("daemon connection failed: %s", exc)
            if not self.stop_event.is_set():
                self.stop_event.wait(delay)
                delay = min(delay * 2, 30)

    def _connect_and_serve(self):
        client = WSClient(
            "codex-watch-bridge",
            sock_path=self.sock_path,
            on_notification=self._on_notification,
            on_server_request=self._on_server_request,
            on_close=self.closed.set,
        )
        with self.lock:
            self.client = client
            self.connected = False
            self.joined.clear()
        try:
            client.request("initialize", {"clientInfo": {
                "name": "codex-watch-bridge",
                "title": "Codex Watch Bridge",
                "version": "0.3.0",
            }}, timeout=20)
            client.notify("initialized")
            self.connected = True
            self._restore_threads()
            self.closed.wait()
        finally:
            with self.lock:
                if self.client is client:
                    self.client = None
                    self.connected = False
                    self.joined.clear()
            client.close()

    def _restore_threads(self):
        result = self.client.request(
            "thread/list",
            {"limit": 100, "sourceKinds": ALL_SOURCE_KINDS},
            timeout=30,
        )
        for item in (result or {}).get("data") or []:
            thread_id = item.get("id")
            if thread_id:
                self.threads[thread_id] = {
                    "cwd": item.get("cwd") or "",
                    "name": item.get("name") or "",
                }
                if (item.get("status") or {}).get("type") == "active":
                    self._join_thread(thread_id)

    def note_thread(self, thread_id, cwd=None, name=None):
        if not thread_id:
            return
        self.threads[str(thread_id)] = {"cwd": cwd or "", "name": name or ""}
        self._join_thread(str(thread_id))

    def _join_thread(self, thread_id):
        if not thread_id or not self.client:
            return
        with self.lock:
            if thread_id in self.joined or thread_id in self.joining:
                return
            self.joining.add(thread_id)
        threading.Thread(
            target=self._join_worker,
            args=(thread_id, self.client),
            daemon=True,
        ).start()

    def _join_worker(self, thread_id, client):
        try:
            for attempt in range(8):
                if self.client is not client or self.stop_event.is_set():
                    return
                try:
                    client.request("thread/resume", {"threadId": thread_id}, timeout=20)
                    with self.lock:
                        if self.client is client:
                            self.joined.add(thread_id)
                    log.info("joined thread %s", thread_id)
                    return
                except Exception as exc:
                    if attempt == 7:
                        log.warning("thread resume failed %s: %s", thread_id, exc)
                    self.stop_event.wait(2)
        finally:
            with self.lock:
                self.joining.discard(thread_id)

    def _on_notification(self, method, params, msg):
        params = params or {}
        thread_id = params.get("threadId")
        if method == "thread/started":
            thread = params.get("thread") or {}
            thread_id = thread.get("id")
            self.note_thread(thread_id, cwd=thread.get("cwd"))
        elif method == "thread/status/changed":
            if (params.get("status") or {}).get("type") == "active":
                self._join_thread(thread_id)
        elif method == "thread/deleted":
            self.threads.pop(thread_id, None)
            with self.lock:
                self.joined.discard(thread_id)
        elif method == "turn/started":
            turn = params.get("turn") or {}
            if self.turn_started_sink:
                self.turn_started_sink(thread_id)
            self._cancel_thread_approvals(thread_id)
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            self._expire_turn(thread_id, turn.get("id"))
        elif method == "serverRequest/resolved":
            self._mark_resolved(thread_id, params.get("requestId"))

    def _on_server_request(self, method, params, msg):
        if method not in APPROVAL_METHODS:
            log.warning("unsupported server request %s", method)
            return
        request_id = msg.get("id")
        thread_id = (params or {}).get("threadId")
        approval_id = uuid.uuid4().hex[:16]
        entry = {
            "approval_id": approval_id,
            "request_id": request_id,
            "thread_id": thread_id,
            "turn_id": (params or {}).get("turnId"),
            "item_id": (params or {}).get("itemId"),
            "method": method,
            "kind": (params or {}).get("kind") or method,
            "command": (params or {}).get("command") or "",
            "cwd": (params or {}).get("cwd") or "",
            "reason": (params or {}).get("reason") or "",
            "available_decisions": (params or {}).get("availableDecisions"),
            "status": "pending",
            "created_at": int(time.time()),
            "timer": None,
        }
        timer = threading.Timer(
            APPROVAL_PUSH_DELAY,
            self._publish_approval,
            args=(approval_id,),
        )
        timer.daemon = True
        entry["timer"] = timer
        with self.lock:
            self.approvals[approval_id] = entry
        self.note_thread(thread_id, cwd=entry["cwd"])
        timer.start()

    def _publish_approval(self, approval_id):
        with self.lock:
            entry = self.approvals.get(approval_id)
            if not entry or entry["status"] != "pending":
                return
            item = {
                "event_id": "appr-" + approval_id,
                "type": "approval_request",
                "title": "APPROVAL NEEDED",
                "body": entry["command"] or entry["reason"] or entry["kind"],
                "payload": {key: value for key, value in entry.items()
                            if key not in ("timer", "status", "created_at", "request_id")},
                "created_at": entry["created_at"],
            }
        if self.notification_sink:
            self.notification_sink(item)

    def _cancel_thread_approvals(self, thread_id):
        with self.lock:
            ids = [
                key for key, item in self.approvals.items()
                if item["thread_id"] == thread_id and item["status"] == "pending"
            ]
        for approval_id in ids:
            self._resolve_approval(approval_id, "resolved", "tui")

    def _mark_resolved(self, thread_id, request_id):
        with self.lock:
            ids = [
                key for key, item in self.approvals.items()
                if item["thread_id"] == thread_id
                and str(item["request_id"]) == str(request_id)
            ]
        for approval_id in ids:
            self._resolve_approval(approval_id, "resolved", "external")

    def _expire_turn(self, thread_id, turn_id):
        with self.lock:
            ids = [
                key for key, item in self.approvals.items()
                if item["thread_id"] == thread_id
                and (not turn_id or item["turn_id"] == turn_id)
                and item["status"] == "pending"
            ]
        for approval_id in ids:
            self._resolve_approval(approval_id, "expired", "turn-complete")

    def _resolve_approval(self, approval_id, status, decided_by):
        with self.lock:
            item = self.approvals.get(approval_id)
            if not item or item["status"] not in ("pending", "sent"):
                return
            item["status"] = status
            item["decided_by"] = decided_by
            item["timer"].cancel()
        if self.notification_cancel_sink:
            self.notification_cancel_sink(approval_id)

    def list_approvals(self, status="pending"):
        with self.lock:
            items = [
                {key: value for key, value in item.items() if key != "timer"}
                for item in self.approvals.values()
                if status == "all" or item["status"] == status
            ]
        return sorted(items, key=lambda item: item["created_at"], reverse=True)[:50]

    def handle_decision(self, approval_id, decision):
        if decision not in VALID_DECISIONS:
            return {"ok": False, "error": "invalid decision"}
        with self.lock:
            item = self.approvals.get(approval_id)
            client = self.client
            if not item:
                return {"ok": False, "error": "approval not found"}
            if item["status"] != "pending":
                return {"ok": False, "error": "approval already " + item["status"]}
            if not client or client.closed:
                return {"ok": False, "error": "not connected to codex daemon"}
            item["status"] = "sending"
        try:
            client.respond(item["request_id"], {"decision": decision})
        except Exception as exc:
            with self.lock:
                item["status"] = "pending"
            return {"ok": False, "error": str(exc)}
        with self.lock:
            item["status"] = "sent"
            item["decision"] = decision
            item["decided_by"] = "watch"
            item["decided_at"] = int(time.time())
            item["timer"].cancel()
        return {"ok": True, "approval_id": approval_id, "decision": decision, "status": "sent"}

    def send_turn(self, thread_id, text):
        client = self.client
        if not client or client.closed:
            return {"ok": False, "error": "not connected to codex daemon"}
        if thread_id not in self.joined:
            return {"ok": False, "error": "thread is not joined"}
        try:
            result = client.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
            }, timeout=30)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "thread_id": thread_id, "turn": result.get("turn") if isinstance(result, dict) else None}
