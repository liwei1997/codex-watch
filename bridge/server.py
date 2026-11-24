#!/usr/bin/env python3
"""
Codex Watch Bridge
Initial version: 0.1.0

Endpoints:
    GET  /health
    GET  /v1/health
    POST /v1/events
    GET  /v1/events
    GET  /v1/events/history
    POST /v1/ack
    GET  /v1/approvals
    POST /v1/approvals
    POST /v1/approvals/<id>/decision

Authentication:
    Authorization: Bearer <TOKEN>
"""

import os
import json
import uuid
import time
import hmac
import sqlite3
import threading
import logging

from pathlib import Path
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import daemon_client


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

DB_PATH = Path(
    os.getenv("CODEX_WATCH_DB", str(DATA_DIR / "events.db"))
)

HOST = os.getenv("CODEX_WATCH_HOST", "127.0.0.1")
PORT = int(os.getenv("CODEX_WATCH_PORT", "8765"))

TOKEN = os.getenv("CODEX_WATCH_TOKEN", "")

VERSION = "0.2.2"
MAX_BODY_SIZE = 1024 * 1024
TASK_COMPLETED_TTL = 60 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("codex-watch")


# --------------------------------------------------
# Database
# --------------------------------------------------

def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")

    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = connect_db()

    conn.execute("PRAGMA journal_mode = WAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,

            source TEXT NOT NULL,
            machine_id TEXT,
            session_id TEXT,

            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,

            payload_json TEXT NOT NULL,

            created_at INTEGER NOT NULL,
            expires_at INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_events_created
        ON events(created_at);

        CREATE TABLE IF NOT EXISTS deliveries (
            event_id TEXT NOT NULL,
            consumer_id TEXT NOT NULL,

            phone_received_at INTEGER,
            watch_received_at INTEGER,

            PRIMARY KEY(event_id, consumer_id),

            FOREIGN KEY(event_id)
                REFERENCES events(event_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            turn_id TEXT,
            item_id TEXT,
            method TEXT,
            kind TEXT,
            command TEXT,
            cwd TEXT,
            reason TEXT,
            available_decisions TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            decision TEXT,
            decided_at INTEGER,
            decided_by TEXT,
            created_at INTEGER NOT NULL,
            UNIQUE(thread_id, request_id)
        );

        CREATE INDEX IF NOT EXISTS idx_approvals_status
        ON approvals(status, created_at);

        CREATE TABLE IF NOT EXISTS daemon_threads (
            thread_id TEXT PRIMARY KEY,
            cwd TEXT,
            name TEXT,
            source TEXT,
            joined_at INTEGER,
            last_active INTEGER
        );
    """)

    # Plan A: notify-hook approvals carry the Codex transcript path so the
    # bridge can watch it for an in-TUI answer and drop the pending push.
    try:
        conn.execute("ALTER TABLE approvals ADD COLUMN transcript_path TEXT")
    except sqlite3.OperationalError:
        pass  # column already present

    # Legacy completion events had no expiry and could flood a watch after a
    # long disconnect. Keep the records, but retire that existing backlog.
    conn.execute("""
        UPDATE events
        SET expires_at = created_at
        WHERE event_type = 'task_completed'
          AND expires_at IS NULL
    """)

    conn.commit()
    conn.close()

    log.info("Database initialized: %s", DB_PATH)


# --------------------------------------------------
# Event storage (shared by the HTTP path and the daemon client)
# --------------------------------------------------

def store_event(data):
    """Validate and insert an event. Returns (event_id, created).

    Raises ValueError on invalid input.
    """
    event_type = str(
        data.get("type", "")
    ).strip()

    if not event_type or len(event_type) > 128:
        raise ValueError("invalid event type")

    event_id = str(
        data.get("event_id") or uuid.uuid4()
    )

    title = str(data.get("title", ""))
    body = str(data.get("body", ""))

    if len(title) > 512 or len(body) > 16384:
        raise ValueError("title or body too large")

    payload = data.get("payload", {})

    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    created_at = int(
        data.get("created_at") or time.time()
    )

    expires_at = data.get("expires_at")

    if expires_at is None and event_type == "task_completed":
        expires_at = created_at + TASK_COMPLETED_TTL
    elif expires_at is not None:
        expires_at = int(expires_at)

    conn = connect_db()

    try:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO events (
                event_id,
                source,
                machine_id,
                session_id,
                event_type,
                title,
                body,
                payload_json,
                created_at,
                expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event_id,
            str(data.get("source", "manual")),
            str(data.get("machine_id", "local")),
            data.get("session_id"),
            event_type,
            title,
            body,
            json.dumps(payload, ensure_ascii=False),
            created_at,
            expires_at
        ))

        created = cursor.rowcount == 1

        conn.commit()

    finally:
        conn.close()

    log.info(
        "Event %s: %s",
        "created" if created else "duplicate",
        event_id
    )

    return event_id, created


# --------------------------------------------------
# Out-of-band approvals (Codex notify hook)
# --------------------------------------------------

NOTIFY_PUSH_DELAY = 60


def store_notify_approval(data):
    """Store a PENDING approval reported by the Codex notify hook
    (approval-requested). The hook does not see the app-server request,
    so the approval id is derived deterministically from thread/turn/kind/
    command and stays idempotent if the hook fires more than once."""
    thread_id = str(data.get("thread_id") or "").strip()
    if not thread_id or len(thread_id) > 128:
        raise ValueError("thread_id required")

    turn_id = str(data.get("turn_id") or "").strip()
    kind = str(data.get("kind") or "command").strip() or "command"
    command = str(data.get("command") or "").strip()
    cwd = str(data.get("cwd") or "").strip()
    reason = str(data.get("reason") or "").strip()
    available = data.get("available_decisions")
    transcript_path = str(data.get("transcript_path") or "").strip()

    import hashlib

    fingerprint = (
        "codex-watch-appr:"
        + thread_id
        + ":" + turn_id
        + ":" + kind
        + ":" + hashlib.sha1(command.encode("utf-8")).hexdigest()[:16]
    )
    approval_id = uuid.uuid5(uuid.NAMESPACE_URL, fingerprint).hex[:16]
    request_id = "notify:" + approval_id
    now = int(time.time())

    conn = connect_db()
    try:
        cur = conn.execute(
            """
            INSERT INTO approvals (
                approval_id, request_id, thread_id, turn_id, item_id,
                method, kind, command, cwd, reason, available_decisions,
                transcript_path, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(thread_id, request_id) DO NOTHING
            """,
            (
                approval_id,
                request_id,
                thread_id,
                turn_id,
                None,
                kind,
                kind,
                command,
                cwd,
                reason,
                json.dumps(available, ensure_ascii=False)
                if available is not None else None,
                transcript_path,
                now,
            ),
        )
        conn.commit()
        fresh = cur.rowcount > 0
    finally:
        conn.close()

    log.info(
        "NOTIFY-APPROVAL pending id=%s thread=%s kind=%s cmd=%s (fresh=%s)",
        approval_id, thread_id, kind, (command or "")[:120], fresh,
    )

    # Join the thread so the daemon replays the still-pending approval
    # server-request to us. Without this, no daemon sibling row is ever
    # created and watch decisions fail with 409.
    try:
        if daemon_client.INSTANCE is not None and thread_id:
            daemon_client.INSTANCE.note_thread(thread_id, cwd=cwd)
    except Exception as exc:
        log.warning("note_thread in store_notify_approval failed: %r", exc)

    # Delay the push: only notify the watch if the approval is still pending
    # after NOTIFY_PUSH_DELAY seconds (i.e. the user has not answered in the
    # Codex terminal). Mirrors the daemon path's grace period.
    if fresh:
        _timer = threading.Timer(
            NOTIFY_PUSH_DELAY,
            _push_notify_approval,
            args=(approval_id, thread_id, turn_id, kind, command, cwd, reason, available),
        )
        _timer.daemon = True
        _timer.start()

        # Plan A: watch the Codex transcript for an in-TUI answer so a
        # request the user already handled in the terminal is never pushed.
        if transcript_path:
            _w = threading.Thread(
                target=_transcript_watcher,
                args=(approval_id, transcript_path),
                name="transcript-watcher-%s" % approval_id,
            )
            _w.daemon = True
            _w.start()

    return {"ok": True, "approval_id": approval_id, "event_id": "appr-" + approval_id}


# Plan A: transcript signals that mean the pending approval was already
# answered in the TUI. Any of these appearing AFTER the hook fired means the
# user handled it locally, so the watch push should be cancelled.
_TUI_ANSWER_ITEM_TYPES = ("CommandExecution", "McpToolCall")
_TUI_ANSWER_EVENT_TYPES = ("turn_aborted", "task_complete")
_TRANSCRIPT_WATCH_INTERVAL = 2
_TRANSCRIPT_WATCH_TIMEOUT = 150


def _transcript_decision_signal(rec):
    """Return a short signal name if this transcript record shows the pending
    approval was answered in the TUI, else None."""
    if not isinstance(rec, dict):
        return None
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return None
    ptype = payload.get("type")
    if ptype in _TUI_ANSWER_EVENT_TYPES:
        return "event:%s" % ptype
    if ptype == "item_completed":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") in _TUI_ANSWER_ITEM_TYPES:
            return "item:%s" % item.get("type")
    return None


def _resolve_notify_by_transcript(approval_id, signal):
    conn = connect_db()
    try:
        cur = conn.execute(
            """
            UPDATE approvals
            SET status = 'resolved',
                decided_at = COALESCE(decided_at, ?),
                decided_by = 'tui'
            WHERE approval_id = ? AND status IN ('pending', 'sent')
            """,
            (int(time.time()), approval_id),
        )
        conn.commit()
        if cur.rowcount:
            log.info(
                "transcript watcher resolved notify approval %s (signal=%s)",
                approval_id, signal,
            )
    finally:
        conn.close()


def _transcript_watcher(approval_id, transcript_path):
    """Tail the Codex transcript for a post-hook decision signal. When the
    user answered the approval in the TUI, resolve the notify row so the
    delayed push (and any already-sent push) is dropped. Read-only."""
    last_size = 0
    try:
        if os.path.exists(transcript_path):
            last_size = os.path.getsize(transcript_path)
    except OSError:
        last_size = 0

    start = time.time()
    while time.time() - start < _TRANSCRIPT_WATCH_TIMEOUT:
        try:
            size = os.path.getsize(transcript_path)
        except OSError:
            size = None
        if size is not None and size > last_size:
            chunk = b""
            try:
                with open(transcript_path, "rb") as fh:
                    fh.seek(last_size)
                    chunk = fh.read(size - last_size)
                last_size = size
            except OSError:
                pass
            if chunk:
                for line in chunk.decode("utf-8", "replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    signal = _transcript_decision_signal(rec)
                    if signal:
                        _resolve_notify_by_transcript(approval_id, signal)
                        return
        elif size is not None and size < last_size:
            # Rotated/truncated: only watch new appends going forward so we
            # never match historical signals from a previous file.
            last_size = size
        time.sleep(_TRANSCRIPT_WATCH_INTERVAL)


def _push_notify_approval(approval_id, thread_id, turn_id, kind, command, cwd, reason, available):
    """Push a notify-hook approval to the watch only if it is still pending and
    no daemon-path row is already covering the same turn (avoids duplicate cards)."""
    conn = connect_db()
    try:
        row = conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if not row or row["status"] != "pending":
            log.info(
                "notify approval %s no longer pending (status=%s), skip push",
                approval_id, row["status"] if row else "missing",
            )
            return
        if turn_id:
            sib = conn.execute(
                """
                SELECT 1 FROM approvals
                WHERE thread_id=? AND turn_id=?
                  AND request_id NOT LIKE 'notify:%'
                  AND status IN ('pending','sent')
                LIMIT 1
                """,
                (thread_id, turn_id),
            ).fetchone()
        else:
            sib = conn.execute(
                """
                SELECT 1 FROM approvals
                WHERE thread_id=?
                  AND request_id NOT LIKE 'notify:%'
                  AND status IN ('pending','sent')
                  AND command=?
                LIMIT 1
                """,
                (thread_id, command),
            ).fetchone()
        if sib:
            log.info("notify approval %s has daemon sibling, skip duplicate push", approval_id)
            return
    finally:
        conn.close()

    project = os.path.basename(cwd) if cwd else "codex"
    body = (command or reason or kind).strip().replace("\n", " ")
    if len(body) > 160:
        body = body[:157] + "..."
    now = int(time.time())

    try:
        store_event({
            "event_id": "appr-" + approval_id,
            "source": "codex-notify",
            "machine_id": "local",
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
            "expires_at": None,
        })
        log.info("notify approval %s pushed to watch (still pending after %ds)", approval_id, NOTIFY_PUSH_DELAY)
    except ValueError as exc:
        log.warning("approval event store failed: %r", exc)


def resolve_thread_approvals(thread_id):
    """A completed turn means every pending approval on that thread was
    already answered in the terminal; resolve them so the watch drops the
    stale notification."""
    if not thread_id:
        return 0
    conn = connect_db()
    try:
        cur = conn.execute(
            """
            UPDATE approvals
            SET status = 'resolved',
                decided_at = COALESCE(decided_at, ?),
                decided_by = COALESCE(decided_by, 'turn-complete')
            WHERE thread_id = ? AND status = 'pending'
            """,
            (int(time.time()), thread_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# --------------------------------------------------
# HTTP helpers
# --------------------------------------------------

def json_bytes(data):
    return json.dumps(
        data,
        ensure_ascii=False
    ).encode("utf-8")


class Handler(BaseHTTPRequestHandler):

    server_version = "CodexWatchBridge/" + VERSION

    def log_message(self, fmt, *args):
        log.info(
            "%s - %s",
            self.client_address[0],
            fmt % args
        )

    def send_json(self, status, data):

        body = json_bytes(data)

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.send_header("Cache-Control", "no-store")

        self.end_headers()

        self.wfile.write(body)

    def authenticate(self):

        authorization = self.headers.get(
            "Authorization", ""
        )

        expected = "Bearer " + TOKEN

        return hmac.compare_digest(
            authorization,
            expected
        )

    def require_auth(self):

        if not self.authenticate():

            self.send_json(401, {
                "ok": False,
                "error": "unauthorized"
            })

            return False

        return True

    def read_json(self):

        try:
            length = int(
                self.headers.get("Content-Length", "0")
            )

        except ValueError:
            raise ValueError("invalid content length")

        if length <= 0 or length > MAX_BODY_SIZE:
            raise ValueError("invalid body size")

        raw = self.rfile.read(length)

        try:
            data = json.loads(raw.decode("utf-8"))

        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid JSON")

        if not isinstance(data, dict):
            raise ValueError("JSON object required")

        return data

    # --------------------------------------------------
    # GET
    # --------------------------------------------------

    def do_GET(self):

        parsed = urlparse(self.path)

        if parsed.path == "/health":

            self.send_json(200, {
                "ok": True,
                "service": "codex-watch-bridge",
                "version": VERSION
            })

            return

        if parsed.path == "/v1/health":

            if not self.require_auth():
                return

            self.send_json(200, {
                "ok": True,
                "service": "codex-watch-bridge",
                "version": VERSION,
                "timestamp": int(time.time())
            })

            return

        if parsed.path == "/v1/events":

            if not self.require_auth():
                return

            params = parse_qs(parsed.query)

            consumer_id = params.get(
                "consumer_id", ["iphone"]
            )[0]

            if not consumer_id or len(consumer_id) > 128:
                self.send_json(400, {
                    "ok": False,
                    "error": "invalid consumer_id"
                })
                return

            try:
                limit = int(
                    params.get("limit", ["20"])[0]
                )
            except ValueError:
                limit = 20

            limit = max(1, min(limit, 100))

            now = int(time.time())

            conn = connect_db()

            rows = conn.execute("""
                SELECT
                    e.*,
                    d.phone_received_at,
                    d.watch_received_at

                FROM events e

                LEFT JOIN deliveries d
                    ON d.event_id = e.event_id
                    AND d.consumer_id = ?

                WHERE
                    (e.expires_at IS NULL OR e.expires_at > ?)
                    AND d.watch_received_at IS NULL
                    AND (
                        e.event_type != 'approval_request'
                        OR EXISTS (
                            SELECT 1
                            FROM approvals a
                            WHERE ('appr-' || a.approval_id) = e.event_id
                              AND a.status = 'pending'
                        )
                    )

                ORDER BY
                    CASE WHEN e.event_type = 'approval_request' THEN 0 ELSE 1 END,
                    e.created_at ASC

                LIMIT ?
            """, (
                consumer_id,
                now,
                limit
            )).fetchall()

            events = []

            for row in rows:

                events.append({
                    "event_id": row["event_id"],
                    "source": row["source"],
                    "machine_id": row["machine_id"],
                    "session_id": row["session_id"],
                    "type": row["event_type"],
                    "title": row["title"],
                    "body": row["body"],
                    "payload": json.loads(
                        row["payload_json"]
                    ),
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "phone_received": (
                        row["phone_received_at"] is not None
                    )
                })

            conn.close()

            self.send_json(200, {
                "ok": True,
                "consumer_id": consumer_id,
                "count": len(events),
                "events": events
            })

            return

        if parsed.path == "/v1/approvals":

            if not self.require_auth():
                return

            params = parse_qs(parsed.query)

            status = params.get("status", ["pending"])[0]

            if status not in (
                "pending", "sent", "resolved", "expired", "all"
            ):
                self.send_json(400, {
                    "ok": False,
                    "error": "invalid status"
                })
                return

            try:
                limit = int(
                    params.get("limit", ["20"])[0]
                )
            except ValueError:
                limit = 20

            limit = max(1, min(limit, 100))

            conn = connect_db()

            if status == "all":
                rows = conn.execute(
                    "SELECT * FROM approvals "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM approvals WHERE status=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status, limit)
                ).fetchall()

            conn.close()

            approvals = []

            for row in rows:

                approvals.append({
                    "approval_id": row["approval_id"],
                    "thread_id": row["thread_id"],
                    "turn_id": row["turn_id"],
                    "method": row["method"],
                    "kind": row["kind"],
                    "command": row["command"],
                    "cwd": row["cwd"],
                    "reason": row["reason"],
                    "status": row["status"],
                    "decision": row["decision"],
                    "actionable": not str(row["request_id"]).startswith("notify:"),
                    "decided_by": row["decided_by"],
                    "created_at": row["created_at"],
                })

            self.send_json(200, {
                "ok": True,
                "count": len(approvals),
                "approvals": approvals
            })

            return

        if parsed.path == "/v1/events/history":

            if not self.require_auth():
                return

            params = parse_qs(parsed.query)

            consumer_id = params.get(
                "consumer_id", ["iphone"]
            )[0]

            if not consumer_id or len(consumer_id) > 128:
                self.send_json(400, {
                    "ok": False,
                    "error": "invalid consumer_id"
                })
                return

            try:
                limit = int(
                    params.get("limit", ["30"])[0]
                )
            except ValueError:
                limit = 30

            limit = max(1, min(limit, 100))

            now = int(time.time())

            conn = connect_db()

            # Newest-first history for the watch inbox.
            # Delivered events always remain visible; undelivered events
            # are only shown while they have not expired (the same rule
            # /v1/events uses for delivery).
            rows = conn.execute("""
                SELECT
                    e.*,
                    d.watch_received_at

                FROM events e

                LEFT JOIN deliveries d
                    ON d.event_id = e.event_id
                    AND d.consumer_id = ?

                WHERE
                    d.watch_received_at IS NOT NULL
                    OR e.expires_at IS NULL
                    OR e.expires_at > ?

                ORDER BY e.created_at DESC

                LIMIT ?
            """, (
                consumer_id,
                now,
                limit
            )).fetchall()

            conn.close()

            events = []

            for row in rows:

                events.append({
                    "event_id": row["event_id"],
                    "source": row["source"],
                    "machine_id": row["machine_id"],
                    "session_id": row["session_id"],
                    "type": row["event_type"],
                    "title": row["title"],
                    "body": row["body"],
                    "payload": json.loads(
                        row["payload_json"]
                    ),
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "delivered": (
                        row["watch_received_at"] is not None
                    ),
                    "watch_received_at": row["watch_received_at"],
                })

            self.send_json(200, {
                "ok": True,
                "consumer_id": consumer_id,
                "count": len(events),
                "events": events
            })

            return

        self.send_json(404, {
            "ok": False,
            "error": "not found"
        })

    # --------------------------------------------------
    # POST
    # --------------------------------------------------

    def do_POST(self):

        parsed = urlparse(self.path)

        if not self.require_auth():
            return

        try:
            data = self.read_json()

        except ValueError as exc:

            self.send_json(400, {
                "ok": False,
                "error": str(exc)
            })

            return

        # ----------------------------------------------
        # POST /v1/events
        # ----------------------------------------------

        if parsed.path == "/v1/events":

            try:
                event_id, created = store_event(data)

            except ValueError as exc:

                self.send_json(400, {
                    "ok": False,
                    "error": str(exc)
                })

                return

            try:
                if daemon_client.INSTANCE is not None:
                    _payload = data.get("payload") or {}
                    _tid = data.get("session_id") or _payload.get("thread_id")
                    if _tid:
                        daemon_client.INSTANCE.note_thread(
                            _tid, cwd=_payload.get("cwd"),
                        )
            except Exception as exc:
                log.warning("note_thread failed: %r", exc)

            try:
                if str(data.get("type", "")) == "task_completed":
                    _tid = data.get("session_id")
                    if _tid:
                        _n = resolve_thread_approvals(_tid)
                        if _n:
                            log.info("resolve approvals on turn-complete: thread=%s n=%d", _tid, _n)
            except Exception as exc:
                log.warning("resolve_thread_approvals failed: %r", exc)

            self.send_json(
                201 if created else 200,
                {
                    "ok": True,
                    "created": created,
                    "event_id": event_id
                }
            )

            return

        # ----------------------------------------------
        # POST /v1/approvals  (notify hook, out-of-band)
        # ----------------------------------------------

        if parsed.path == "/v1/approvals":

            try:
                result = store_notify_approval(data)

            except ValueError as exc:

                self.send_json(400, {
                    "ok": False,
                    "error": str(exc)
                })

                return

            self.send_json(201, result)

            return

        # ----------------------------------------------
        # POST /v1/approvals/<id>/decision
        # ----------------------------------------------

        parts = parsed.path.split("/")

        if (
            len(parts) == 5
            and parts[1] == "v1"
            and parts[2] == "approvals"
            and parts[4] == "decision"
        ):

            approval_id = parts[3]

            if not approval_id or len(approval_id) > 64:

                self.send_json(400, {
                    "ok": False,
                    "error": "invalid approval id"
                })

                return

            decision = str(
                data.get("decision", "")
            ).strip()

            if daemon_client.INSTANCE is None:

                self.send_json(503, {
                    "ok": False,
                    "error": "daemon client not available"
                })

                return

            result = daemon_client.INSTANCE.handle_decision(
                approval_id,
                decision
            )

            self.send_json(
                200 if result.get("ok") else 409,
                result
            )

            return

        # ----------------------------------------------
        # POST /v1/ack
        # ----------------------------------------------

        if parsed.path == "/v1/ack":

            event_id = str(
                data.get("event_id", "")
            )

            consumer_id = str(
                data.get("consumer_id", "iphone")
            )

            stage = data.get("stage")

            if stage not in (
                "phone_received",
                "watch_received"
            ):

                self.send_json(400, {
                    "ok": False,
                    "error": "invalid ack stage"
                })

                return

            if not event_id or not consumer_id:

                self.send_json(400, {
                    "ok": False,
                    "error": "event_id and consumer_id required"
                })

                return

            now = int(time.time())

            conn = connect_db()

            exists = conn.execute("""
                SELECT 1 FROM events
                WHERE event_id = ?
            """, (event_id,)).fetchone()

            if not exists:

                conn.close()

                self.send_json(404, {
                    "ok": False,
                    "error": "event not found"
                })

                return

            if stage == "phone_received":

                conn.execute("""
                    INSERT INTO deliveries (
                        event_id,
                        consumer_id,
                        phone_received_at
                    )
                    VALUES (?, ?, ?)

                    ON CONFLICT(event_id, consumer_id)
                    DO UPDATE SET
                        phone_received_at =
                            COALESCE(
                                deliveries.phone_received_at,
                                excluded.phone_received_at
                            )
                """, (
                    event_id,
                    consumer_id,
                    now
                ))

            else:

                conn.execute("""
                    INSERT INTO deliveries (
                        event_id,
                        consumer_id,
                        watch_received_at
                    )
                    VALUES (?, ?, ?)

                    ON CONFLICT(event_id, consumer_id)
                    DO UPDATE SET
                        watch_received_at =
                            COALESCE(
                                deliveries.watch_received_at,
                                excluded.watch_received_at
                            )
                """, (
                    event_id,
                    consumer_id,
                    now
                ))

            conn.commit()
            conn.close()

            self.send_json(200, {
                "ok": True,
                "event_id": event_id,
                "consumer_id": consumer_id,
                "stage": stage,
                "timestamp": now
            })

            return

        self.send_json(404, {
            "ok": False,
            "error": "not found"
        })


# --------------------------------------------------
# Main
# --------------------------------------------------

def main():

    if not TOKEN or len(TOKEN) < 32:
        raise SystemExit(
            "ERROR: Set CODEX_WATCH_TOKEN "
            "to a random secret of at least 32 characters."
        )

    init_db()

    if os.getenv("CODEX_WATCH_DISABLE_DAEMON_CLIENT") != "1":

        daemon_client.INSTANCE = daemon_client.DaemonClient(
            db_path=DB_PATH,
            event_sink=store_event,
        )

        daemon_client.INSTANCE.start()

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler
    )

    log.info("Codex Watch Bridge v%s", VERSION)
    log.info("Listening on http://%s:%s", HOST, PORT)
    log.info("Database: %s", DB_PATH)

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        log.info("Shutting down")

    finally:
        server.server_close()


if __name__ == "__main__":
    main()
