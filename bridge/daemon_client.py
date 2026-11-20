#!/usr/bin/env python3
"""
Codex app-server daemon client for the Codex Watch Bridge.

Runs as a background thread inside the bridge process. It joins live codex
threads as a *secondary* client (the TUI stays the primary client), forwards
approval server-requests into the approvals table + events pipeline, and
relays decisions from the watch back to the daemon.

Protocol facts (all verified by the 2026-11-16 approval spike):
  - A non-owner client can join a live thread with
    thread/resume {threadId}. The rollout file needs ~2s after thread
    creation; earlier attempts fail with "no rollout found" -> retry.
  - Approval server-requests are broadcast to every connected client of the
    thread; the FIRST response resolves the request for everyone.
  - A client that joins AFTER the request was issued still receives
    still-pending approval requests (daemon replay) -> restarting or
    reconnecting the bridge never loses a pending approval.
  - thread/status/changed is a GLOBAL broadcast (no thread membership
    needed). Status {"type":"active","activeFlags":["waitingOnApproval"]}
    marks a thread with a pending approval.
  - serverRequest/resolved {threadId, requestId} broadcasts the resolution.
  - thread/list indexes TUI/cli threads (source="cli" / "vscode") with
    current status (idle / active / notLoaded).
"""
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid

from wsclient import WSClient

log = logging.getLogger("codex-watch.daemon")

DAEMON_SOCK = os.environ.get(
    "CODEX_DAEMON_SOCK",
    "/home/liwei/.codex/app-server-control/app-server-control.sock",
)

MACHINE_ID = os.getenv("CODEX_WATCH_MACHINE_ID", "ubuntu-main")

ALL_SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "subAgent", "unknown"]

# Server-request methods that map to a watch-able approval.
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "execCommandApproval",
    "item/fileChange/requestApproval",
    "applyPatchApproval",
    "item/permissions/requestApproval",
}

VALID_DECISIONS = ("accept", "decline", "cancel")

EVENT_TTL = 3600  # approval notifications expire after 1 hour
REJOIN_LIMIT = 20  # max persisted threads to rejoin on reconnect


def _connect_db(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _bindable(value):
    """Coerce a value to something sqlite3 can bind.

    thread/list fields like 'name' can be dicts/lists; sqlite3 cannot
    bind those, so fall back to a string representation.
    """
    if value is None or isinstance(value, (int, float, str, bytes)):
        return value
    try:
        return str(value)
    except Exception:
        return None


class DaemonClient:
    def __init__(self, db_path, event_sink=None, sock_path=DAEMON_SOCK):
        self.db_path = db_path
        self.event_sink = event_sink  # fn(event_fields: dict) -> None
        self.sock_path = sock_path
        self.client = None            # current WSClient (None while offline)
        self.joined = set()           # thread ids this connection has joined
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._thread = None
        self._join_attempts = set()   # thread ids with a join worker running
        self._attempts_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(
            target=self._run_forever,
            name="codex-daemon-client",
            daemon=True,
        )
        self._thread.start()
        log.info("daemon client started (sock=%s)", self.sock_path)

    def stop(self):
        self._stop.set()
        if self.client:
            self.client.close()

    def _run_forever(self):
        backoff = 2
        while not self._stop.is_set():
            self._closed.clear()
            try:
                self._connect_and_serve()
                backoff = 2
            except Exception as e:
                log.warning("daemon connection failed: %r", e)
            if self._stop.is_set():
                break
            log.info("daemon reconnect in %ss", backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30)

    def _connect_and_serve(self):
        client = WSClient(
            "codex-watch-bridge",
            sock_path=self.sock_path,
            on_notification=self._on_notification,
            on_server_request=self._on_server_request,
            on_close=lambda: self._closed.set(),
        )
        self.client = client
        try:
            r = client.request(
                "initialize",
                {"clientInfo": {
                    "name": "codex-watch-bridge",
                    "title": "Codex Watch Bridge",
                    "version": "0.2.0",
                }},
                timeout=20,
            )
            client.notify("initialized")
            log.info("daemon connection ready")
            try:
                self._restore_threads()
            except Exception as e:
                log.warning("thread restore failed: %r", e)
            # Block until the reader signals close.
            self._closed.wait()
        finally:
            client.close()
            if self.client is client:
                self.client = None
            log.info("daemon connection closed")

    # ------------------------------------------------------------------
    # Thread membership
    # ------------------------------------------------------------------

    def _restore_threads(self):
        """Join live threads after (re)connect and refresh the persisted set."""
        now = int(time.time())
        try:
            r = self.client.request(
                "thread/list",
                {"limit": 100, "sourceKinds": ALL_SOURCE_KINDS},
                timeout=30,
            )
        except Exception as e:
            log.warning("thread/list failed: %r", e)
            r = None
        data = ((r or {}).get("data")) or []
        conn = _connect_db(self.db_path)
        try:
            for t in data:
                tid = t.get("id")
                if not tid:
                    continue
                st = ((t.get("status") or {}).get("type")) or "unknown"
                conn.execute(
                    """
                    INSERT INTO daemon_threads (
                        thread_id, cwd, name, source, joined_at, last_active
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        cwd = COALESCE(excluded.cwd, daemon_threads.cwd),
                        name = COALESCE(excluded.name, daemon_threads.name),
                        source = COALESCE(excluded.source, daemon_threads.source),
                        last_active = excluded.last_active
                    """,
                    (
                        tid,
                        _bindable(t.get("cwd")),
                        _bindable(t.get("name")),
                        _bindable(t.get("source")),
                        now,
                        now,
                    ),
                )
                if st == "active":
                    self._join_thread(tid)
            # Rejoin threads we were watching in a previous run.
            rows = conn.execute(
                """
                SELECT thread_id FROM daemon_threads
                ORDER BY last_active DESC LIMIT ?
                """,
                (REJOIN_LIMIT,),
            ).fetchall()
            for row in rows:
                self._join_thread(row["thread_id"])
            # Pending approvals whose thread is no longer active cannot be
            # resolved anymore (daemon restarted / turn gone).
            active_ids = {
                t.get("id") for t in data
                if ((t.get("status") or {}).get("type")) == "active"
            }
            pend = conn.execute(
                "SELECT approval_id, thread_id FROM approvals WHERE status='pending'"
            ).fetchall()
            for p in pend:
                if p["thread_id"] not in active_ids:
                    conn.execute(
                        "UPDATE approvals SET status='expired' WHERE approval_id=?",
                        (p["approval_id"],),
                    )
                    log.info("approval %s expired (thread not active)", p["approval_id"])
            conn.commit()
        finally:
            conn.close()

    def _join_thread(self, thread_id):
        """Join a thread, with rollout-wait retries, without blocking the WS reader."""
        if not thread_id or self.client is None:
            return
        with self._attempts_lock:
            if thread_id in self.joined or thread_id in self._join_attempts:
                return
            self._join_attempts.add(thread_id)
        threading.Thread(
            target=self._join_worker, args=(thread_id,), daemon=True,
            name="join-%s" % thread_id[:8],
        ).start()

    def _join_worker(self, thread_id):
        client = self.client
        try:
            for attempt in range(8):
                if self.client is not client or self._stop.is_set():
                    return  # reconnect happened; new connection restores
                try:
                    client.request("thread/resume", {"threadId": thread_id}, timeout=20)
                    self.joined.add(thread_id)
                    now = int(time.time())
                    conn = _connect_db(self.db_path)
                    try:
                        conn.execute(
                            "UPDATE daemon_threads SET joined_at=?, last_active=? WHERE thread_id=?",
                            (now, now, thread_id),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    log.info("JOINED thread %s", thread_id)
                    return
                except Exception as e:
                    log.info("join %s attempt %d failed: %s", thread_id[:8], attempt + 1, e)
                    if self._stop.is_set():
                        return
                    time.sleep(2)  # rollout file may not exist yet (~2s after thread start)
        finally:
            with self._attempts_lock:
                self._join_attempts.discard(thread_id)

    def _drop_thread(self, thread_id):
        if not thread_id:
            return
        self.joined.discard(thread_id)
        conn = _connect_db(self.db_path)
        try:
            conn.execute("DELETE FROM daemon_threads WHERE thread_id=?", (thread_id,))
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def note_thread(self, thread_id, cwd=None, name=None):
        """Learn a thread out-of-band (codex_notify carries its id) and join it
        by explicit id. thread/list + connect-time status dump only cover
        threads fully resolved in the daemon index; a session pinned to a custom
        model provider is dropped there, so its status is never broadcast and
        the bridge would never join it. thread/resume by id only needs the
        provider and succeeds when not mid-turn. Upsert persists the id so a
        reconnect rejoins it via the DB top-N path."""
        if not thread_id:
            return
        now = int(time.time())
        conn = _connect_db(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO daemon_threads (
                    thread_id, cwd, name, source, joined_at, last_active
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    cwd = COALESCE(excluded.cwd, daemon_threads.cwd),
                    name = COALESCE(excluded.name, daemon_threads.name),
                    last_active = excluded.last_active
                """,
                (thread_id, _bindable(cwd), _bindable(name), "notified", now, now),
            )
            conn.commit()
        finally:
            conn.close()
        log.info("note_thread: %s cwd=%s", thread_id, cwd)
        self._join_thread(thread_id)

    # Daemon event handling (runs on the WS reader thread: keep it fast)
    # ------------------------------------------------------------------

    def _on_notification(self, method, params, msg):
        params = params or {}
        if method == "thread/status/changed":
            st = params.get("status") or {}
            if st.get("type") == "active":
                # Global broadcast: covers idle->active (new turn in the TUI)
                # and waitingOnApproval flags.
                self._join_thread(params.get("threadId"))
        elif method == "thread/started":
            th = params.get("thread") or {}
            self._join_thread(th.get("id"))
        elif method == "thread/deleted":
            self._drop_thread(params.get("threadId"))
        elif method == "serverRequest/resolved":
            self._mark_resolved(params.get("threadId"), params.get("requestId"))
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            self._expire_turn(params.get("threadId"), turn.get("id"))

    def _on_server_request(self, method, params, msg):
        if method not in APPROVAL_METHODS:
            # Unknown server request: answer with cancel/empty to avoid
            # hanging the turn, and log it.
            log.warning("unhandled server request %s id=%s", method, msg.get("id"))
            self.client.respond(msg.get("id"), {"decision": "cancel"})
            return
        threading.Thread(
            target=self._store_approval,
            args=(method, params or {}, msg),
            daemon=True,
            name="store-approval",
        ).start()

    # ------------------------------------------------------------------
    # Approval storage / lifecycle
    # ------------------------------------------------------------------

    def _store_approval(self, method, p, msg):
        request_id = msg.get("id")
        thread_id = p.get("threadId")
        turn_id = p.get("turnId")
        kind = p.get("kind") or method
        command = p.get("command") or ""
        cwd = p.get("cwd") or ""
        reason = p.get("reason") or ""
        available = p.get("availableDecisions")
        approval_id = uuid.uuid4().hex[:16]
        now = int(time.time())

        conn = _connect_db(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO approvals (
                    approval_id, request_id, thread_id, turn_id, item_id,
                    method, kind, command, cwd, reason, available_decisions,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(thread_id, request_id) DO UPDATE SET
                    status = 'pending',
                    decision = NULL,
                    decided_at = NULL,
                    decided_by = NULL,
                    command = excluded.command,
                    reason = excluded.reason,
                    created_at = excluded.created_at
                """,
                (
                    approval_id,
                    request_id,
                    thread_id,
                    turn_id,
                    p.get("itemId"),
                    method,
                    kind,
                    command,
                    cwd,
                    reason,
                    json.dumps(available, ensure_ascii=False) if available is not None else None,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        log.info(
            "APPROVAL pending id=%s thread=%s kind=%s cmd=%s",
            approval_id, thread_id, kind, (command or "")[:120],
        )

        if self.event_sink:
            project = os.path.basename(cwd) if cwd else "codex"
            body = (command or reason or method).strip().replace("\n", " ")
            if len(body) > 160:
                body = body[:157] + "..."
            try:
                self.event_sink({
                    "event_id": "appr-" + approval_id,
                    "source": "daemon-client",
                    "machine_id": MACHINE_ID,
                    "session_id": thread_id,
                    "type": "approval_request",
                    "title": "APPROVAL NEEDED",
                    "body": f"{project}: {body}",
                    "payload": {
                        "approval_id": approval_id,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "kind": kind,
                        "command": command,
                        "cwd": cwd,
                        "reason": reason,
                        "available_decisions": available,
                    },
                    "created_at": now,
                    "expires_at": now + EVENT_TTL,
                })
            except Exception as e:
                log.warning("approval event push failed: %r", e)

    def _mark_resolved(self, thread_id, request_id):
        """serverRequest/resolved: someone (terminal or watch) answered."""
        if thread_id is None and request_id is None:
            return
        conn = _connect_db(self.db_path)
        try:
            cur = conn.execute(
                """
                UPDATE approvals
                SET status='resolved',
                    decided_at=COALESCE(decided_at, ?),
                    decided_by=COALESCE(decided_by, 'external')
                WHERE thread_id=? AND request_id=? AND status IN ('pending','sent')
                """,
                (int(time.time()), thread_id, request_id),
            )
            conn.commit()
            if cur.rowcount:
                log.info("APPROVAL resolved thread=%s request_id=%s", thread_id, request_id)
        finally:
            conn.close()

    def _expire_turn(self, thread_id, turn_id):
        """Turn finished: any still-pending approval of that turn is gone."""
        if not thread_id:
            return
        conn = _connect_db(self.db_path)
        try:
            if turn_id:
                conn.execute(
                    "UPDATE approvals SET status='expired' WHERE thread_id=? AND turn_id=? AND status IN ('pending','sent')",
                    (thread_id, turn_id),
                )
            else:
                conn.execute(
                    "UPDATE approvals SET status='expired' WHERE thread_id=? AND status IN ('pending','sent')",
                    (thread_id,),
                )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Decision entry point (called from the HTTP layer)
    # ------------------------------------------------------------------

    def handle_decision(self, approval_id, decision):
        """Send the watch's decision to the daemon. Returns a dict for HTTP."""
        if decision not in VALID_DECISIONS:
            return {"ok": False, "error": "decision must be one of %s" % "|".join(VALID_DECISIONS)}
        client = self.client
        if client is None or client.closed:
            return {"ok": False, "error": "not connected to codex daemon"}

        conn = _connect_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return {"ok": False, "error": "approval not found"}
        if row["status"] not in ("pending", "sent"):
            return {"ok": False, "error": "approval already %s" % row["status"]}

        try:
            # Daemon server-request id is a JSON integer, but request_id is stored
            # in a TEXT column (SQLite TEXT affinity coerces it to a string). Echo it
            # back as an int or the daemon cannot match the response and drops it.
            rid = row["request_id"]
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                pass
            client.respond(rid, {"decision": decision})
        except Exception as e:
            log.warning("respond failed for %s: %r", approval_id, e)
            return {"ok": False, "error": "failed to send decision to daemon"}

        now = int(time.time())
        conn = _connect_db(self.db_path)
        try:
            conn.execute(
                """
                UPDATE approvals
                SET status='sent', decision=?, decided_at=?, decided_by='watch'
                WHERE approval_id=?
                """,
                (decision, now, approval_id),
            )
            conn.commit()
        finally:
            conn.close()
        log.info("DECISION sent id=%s decision=%s", approval_id, decision)
        return {
            "ok": True,
            "approval_id": approval_id,
            "decision": decision,
            "status": "sent",
        }


# Module-level singleton (set by server.py at startup).
INSTANCE = None
