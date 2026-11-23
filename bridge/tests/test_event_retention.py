import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import server


class EventRetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_data_dir = server.DATA_DIR
        self.original_db_path = server.DB_PATH
        self.original_token = server.TOKEN
        server.DATA_DIR = Path(self.temp_dir.name)
        server.DB_PATH = server.DATA_DIR / "events.db"
        server.TOKEN = "test-token-with-at-least-32-characters"
        server.init_db()

    def tearDown(self):
        server.DATA_DIR = self.original_data_dir
        server.DB_PATH = self.original_db_path
        server.TOKEN = self.original_token
        self.temp_dir.cleanup()

    def get_events(self):
        handler = object.__new__(server.Handler)
        handler.path = "/v1/events?consumer_id=iphone"
        handler.headers = {
            "Authorization": "Bearer " + server.TOKEN,
        }
        response = {}

        def capture(status, data):
            response["status"] = status
            response["data"] = data

        handler.send_json = capture
        handler.do_GET()

        self.assertEqual(response["status"], 200)
        return response["data"]["events"]

    def test_init_expires_legacy_task_completion_backlog(self):
        created_at = 1_700_000_000

        with sqlite3.connect(server.DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO events (
                    event_id, source, machine_id, session_id,
                    event_type, title, body, payload_json,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-completion",
                    "codex",
                    "workstation",
                    "thread-1",
                    "task_completed",
                    "Codex finished",
                    "Old result",
                    "{}",
                    created_at,
                    None,
                ),
            )

        server.init_db()

        with sqlite3.connect(server.DB_PATH) as conn:
            expires_at = conn.execute(
                "SELECT expires_at FROM events WHERE event_id = ?",
                ("legacy-completion",),
            ).fetchone()[0]

        self.assertEqual(expires_at, created_at)

    def test_new_task_completion_expires_after_one_hour(self):
        created_at = 1_700_000_000

        server.store_event({
            "event_id": "new-completion",
            "source": "codex",
            "machine_id": "workstation",
            "session_id": "thread-2",
            "type": "task_completed",
            "title": "Codex finished",
            "body": "Fresh result",
            "payload": {},
            "created_at": created_at,
        })

        with sqlite3.connect(server.DB_PATH) as conn:
            expires_at = conn.execute(
                "SELECT expires_at FROM events WHERE event_id = ?",
                ("new-completion",),
            ).fetchone()[0]

        self.assertEqual(expires_at, created_at + 3_600)

    def test_resolved_approval_is_not_delivered(self):
        now = 1_800_000_000

        with sqlite3.connect(server.DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    approval_id, request_id, thread_id, status, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("resolved-1", "41", "thread-3", "resolved", now),
            )
            conn.execute(
                """
                INSERT INTO events (
                    event_id, source, machine_id, session_id,
                    event_type, title, body, payload_json,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "appr-resolved-1",
                    "daemon-client",
                    "workstation",
                    "thread-3",
                    "approval_request",
                    "APPROVAL NEEDED",
                    "Already resolved",
                    "{}",
                    now,
                    now + 3_600,
                ),
            )

        self.assertEqual(self.get_events(), [])

    def test_pending_approval_precedes_older_completion(self):
        server.store_event({
            "event_id": "older-completion",
            "source": "codex",
            "machine_id": "workstation",
            "session_id": "thread-queue",
            "type": "task_completed",
            "title": "Done",
            "body": "older message",
            "payload": {},
        })
        result = server.store_notify_approval({
            "thread_id": "thread-priority",
            "turn_id": "turn-priority",
            "kind": "command",
            "command": "git push",
            "cwd": "/tmp/project",
        })
        ids = [event["event_id"] for event in self.get_events()]
        self.assertEqual(ids[0], result["event_id"])
        self.assertIn("older-completion", ids)

    def test_pending_approval_remains_deliverable(self):
        now = 1_800_000_000

        with sqlite3.connect(server.DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    approval_id, request_id, thread_id, status, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("pending-1", "42", "thread-4", "pending", now),
            )
            conn.execute(
                """
                INSERT INTO events (
                    event_id, source, machine_id, session_id,
                    event_type, title, body, payload_json,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "appr-pending-1",
                    "daemon-client",
                    "workstation",
                    "thread-4",
                    "approval_request",
                    "APPROVAL NEEDED",
                    "Still pending",
                    "{}",
                    now,
                    now + 3_600,
                ),
            )

        events = self.get_events()

        self.assertEqual(
            [event["event_id"] for event in events],
            ["appr-pending-1"],
        )


if __name__ == "__main__":
    unittest.main()
