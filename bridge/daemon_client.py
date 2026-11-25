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

INSTANCE = None
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
        turn_completed_sink=None,
        notification_cancel_sink=None,
        sock_path=DAEMON_SOCK,
    ):
        self.notification_sink = notification_sink
        self.turn_started_sink = turn_started_sink
        self.turn_completed_sink = turn_completed_sink
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
        self.activity = {}
        self.last_completed = {}

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
            self.activity.clear()
        try:
            client.request("initialize", {"clientInfo": {
                "name": "codex-watch-bridge",
                "title": "Codex Watch Bridge",
                "version": "0.4.1",
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

    def _loaded_thread_ids(self, client):
        loaded = set()
        cursor = None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = client.request("thread/loaded/list", params, timeout=30) or {}
            loaded.update(result.get("data") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return loaded

    def _restore_threads(self):
        client = self.client
        result = client.request(
            "thread/list",
            {"limit": 100, "sourceKinds": ALL_SOURCE_KINDS},
            timeout=30,
        )
        loaded = self._loaded_thread_ids(client)
        for item in (result or {}).get("data") or []:
            thread_id = item.get("id")
            if not thread_id:
                continue
            self.note_thread(thread_id, cwd=item.get("cwd"), name=item.get("name"))
            if thread_id in loaded and (item.get("status") or {}).get("type") == "active":
                with self.lock:
                    self.activity.setdefault(thread_id, ("active", time.time(), object()))
        for thread_id in loaded:
            self._join_thread(thread_id)

    def _mark_joined(self, thread_id, client=None):
        if not thread_id:
            return
        with self.lock:
            if client is None or self.client is client:
                self.joined.add(str(thread_id))

    def note_thread(self, thread_id, cwd=None, name=None):
        if not thread_id:
            return
        thread_id = str(thread_id)
        current = self.threads.get(thread_id) or {}
        self.threads[thread_id] = {
            "cwd": cwd or current.get("cwd") or "",
            "name": name or current.get("name") or "",
        }

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

    def _ensure_joined(self, thread_id, client):
        with self.lock:
            if thread_id in self.joined:
                return
        result = client.request("thread/resume", {
            "threadId": thread_id,
            "excludeTurns": True,
        }, timeout=20)
        thread = (result or {}).get("thread") or {}
        self.note_thread(thread_id, cwd=thread.get("cwd"), name=thread.get("name"))
        self._mark_joined(thread_id, client)

    def _join_worker(self, thread_id, client):
        try:
            for attempt in range(8):
                if self.client is not client or self.stop_event.is_set():
                    return
                try:
                    self._ensure_joined(thread_id, client)
                    log.info("joined thread %s", thread_id)
                    return
                except Exception as exc:
                    if attempt == 7 or "active writer" in str(exc):
                        log.warning("thread resume failed %s: %s", thread_id, exc)
                        return
                    self.stop_event.wait(2)
        finally:
            with self.lock:
                self.joining.discard(thread_id)

    def _set_active(self, thread_id, msg):
        if not thread_id:
            return
        with self.lock:
            previous = self.activity.get(thread_id)
            if previous and previous[0] == "active":
                return
            started = (msg.get("emittedAtMs") or time.time() * 1000) / 1000
            self.activity[thread_id] = ("active", started, object())
            if self.turn_started_sink:
                self.turn_started_sink(thread_id)

    def _set_idle(self, thread_id):
        with self.lock:
            previous = self.activity.get(thread_id)
            if not previous or previous[0] != "active":
                return
            state = ("idle", previous[1], previous[2])
            self.activity[thread_id] = state
            client = self.client
        if client:
            threading.Thread(
                target=self._recover_completion,
                args=(thread_id, client, state),
                daemon=True,
            ).start()

    def _recover_completion(self, thread_id, client, state):
        for attempt in range(5):
            with self.lock:
                if self.client is not client or self.activity.get(thread_id) != state:
                    return
            try:
                result = client.request("thread/turns/list", {
                    "threadId": thread_id,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "full",
                }, timeout=10) or {}
                turns = result.get("data") or []
                turn = turns[0] if turns else {}
                completed = turn.get("completedAt")
                if turn.get("status") == "completed" and completed is not None and completed >= int(state[1]):
                    with self.lock:
                        if self.client is client and self.activity.get(thread_id) == state:
                            self._complete_turn(thread_id, turn)
                    return
                if turn.get("status") in ("failed", "interrupted") and completed is not None and completed >= int(state[1]):
                    return
            except Exception as exc:
                log.warning("latest turn lookup failed %s: %s", thread_id, exc)
            if self.stop_event.wait(1):
                return
        log.warning("no recent completed turn found for %s", thread_id)

    def _complete_turn(self, thread_id, turn):
        turn_id = turn.get("id")
        if not thread_id or not turn_id:
            return
        with self.lock:
            self._expire_turn(thread_id, turn_id)
            if not self.turn_completed_sink or turn.get("status", "completed") != "completed":
                return
            if self.last_completed.get(thread_id) == turn_id:
                return
            state = self.activity.get(thread_id)
            completed = turn.get("completedAt")
            if state and completed is not None and completed < int(state[1]):
                return
            message = next((
                str(item.get("text") or "").strip()
                for item in reversed(turn.get("items") or [])
                if isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and item.get("phase") in (None, "final_answer")
                and item.get("text")
            ), "")
            thread = self.threads.get(thread_id) or {}
            try:
                self.turn_completed_sink({
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "cwd": thread.get("cwd") or "",
                    "message": (message or "Codex turn completed.")[:16000],
                })
                self.last_completed[thread_id] = turn_id
                log.info("completion notification scheduled thread=%s turn=%s", thread_id, turn_id)
            except Exception:
                log.exception("completion notification scheduling failed thread=%s turn=%s", thread_id, turn_id)

    def _on_notification(self, method, params, msg):
        params = params or {}
        thread_id = params.get("threadId")
        if method == "thread/started":
            thread = params.get("thread") or {}
            thread_id = thread.get("id")
            self.note_thread(thread_id, cwd=thread.get("cwd"))
            self._join_thread(thread_id)
        elif method == "thread/status/changed":
            status = (params.get("status") or {}).get("type")
            if status == "active":
                self.note_thread(thread_id)
                self._set_active(thread_id, msg)
                self._join_thread(thread_id)
            elif status == "idle":
                self._set_idle(thread_id)
            elif status == "notLoaded":
                with self.lock:
                    self.joined.discard(thread_id)
                    self.activity.pop(thread_id, None)
        elif method == "thread/deleted":
            with self.lock:
                self.threads.pop(thread_id, None)
                self.joined.discard(thread_id)
                self.activity.pop(thread_id, None)
                self.last_completed.pop(thread_id, None)
                if self.turn_started_sink:
                    self.turn_started_sink(thread_id)
        elif method == "turn/started":
            self._set_active(thread_id, msg)
            self._cancel_thread_approvals(thread_id)
        elif method == "turn/completed":
            self._complete_turn(thread_id, params.get("turn") or {})
        elif method == "serverRequest/resolved":
            self._mark_resolved(thread_id, params.get("requestId"))

    def _on_server_request(self, method, params, msg):
        if method not in APPROVAL_METHODS:
            log.warning("unsupported server request %s", method)
            client = self.client
            if client:
                client.respond(msg.get("id"), {"decision": "cancel"})
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
            if any(
                item["thread_id"] == thread_id
                and str(item["request_id"]) == str(request_id)
                and item["status"] in ("pending", "sent")
                for item in self.approvals.values()
            ):
                return
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
        try:
            if thread_id not in self._loaded_thread_ids(client):
                self._ensure_joined(thread_id, client)
            result = client.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
            }, timeout=30)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "thread_id": thread_id, "turn": result.get("turn") if isinstance(result, dict) else None}
