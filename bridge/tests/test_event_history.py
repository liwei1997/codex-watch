import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path


BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import server


def insert_event(conn, event_id, created_at, expires_at=None,
                 event_type="task_completed", title="t", body="b"):
    conn.execute(
        """
        INSERT INTO events (
            event_id, source, machine_id, session_id,
            event_type, title, body, payload_json,
            created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            "codex",
            "workstation",
            "thread-1",
            event_type,
            title,
            body,
            "{}",
            created_at,
            expires_at,
        ),
    )


class EventHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_data_dir = server.DATA_DIR
        self.original_db_path = server.DB_PATH
        self.original_token = server.TOKEN
        server.DATA_DIR = Path(self.temp_dir.name)
        server.DB_PATH = server.DATA_DIR / "events.db"
        server.TOKEN = "test-token-with-at-least-32-characters"
        server.init_db()
        self.now = int(time.time())

    def tearDown(self):
        server.DATA_DIR = self.original_data_dir
        server.DB_PATH = self.original_db_path
        server.TOKEN = self.original_token
        self.temp_dir.cleanup()

    def get_history(self, consumer_id="iphone", limit=None):
        path = "/v1/events/history?consumer_id=" + consumer_id
        if limit is not None:
            path += "&limit=" + str(limit)

        handler = object.__new__(server.Handler)
        handler.path = path
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
        return response["data"]

    def deliver(self, event_id, consumer_id="iphone", at=None):
        with sqlite3.connect(server.DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO deliveries (
                    event_id, consumer_id, watch_received_at
                ) VALUES (?, ?, ?)
                """,
                (event_id, consumer_id, at or self.now),
            )

    def test_history_is_newest_first(self):
        with sqlite3.connect(server.DB_PATH) as conn:
            insert_event(conn, "old", self.now - 200)
            insert_event(conn, "mid", self.now - 100)
            insert_event(conn, "new", self.now - 10)

        data = self.get_history()
        self.assertEqual(
            [e["event_id"] for e in data["events"]],
            ["new", "mid", "old"],
        )

    def test_delivered_flag(self):
        with sqlite3.connect(server.DB_PATH) as conn:
            insert_event(conn, "delivered-1", self.now - 100)
            insert_event(conn, "undelivered-1", self.now - 50)

        self.deliver("delivered-1")

        data = self.get_history()
        events = {e["event_id"]: e for e in data["events"]}

        self.assertTrue(events["delivered-1"]["delivered"])
        self.assertIsNotNone(events["delivered-1"]["watch_received_at"])
        self.assertFalse(events["undelivered-1"]["delivered"])
        self.assertIsNone(events["undelivered-1"]["watch_received_at"])

    def test_expired_undelivered_events_are_hidden(self):
        with sqlite3.connect(server.DB_PATH) as conn:
            insert_event(
                conn, "expired-hidden", self.now - 100,
                expires_at=self.now - 10,
            )
            insert_event(conn, "live-shown", self.now - 100)

        data = self.get_history()
        self.assertEqual(
            [e["event_id"] for e in data["events"]],
            ["live-shown"],
        )

    def test_delivered_events_stay_visible_after_expiry(self):
        with sqlite3.connect(server.DB_PATH) as conn:
            insert_event(
                conn, "old-delivered", self.now - 100,
                expires_at=self.now - 10,
            )

        self.deliver("old-delivered", at=self.now - 20)

        data = self.get_history()
        self.assertEqual(
            [e["event_id"] for e in data["events"]],
            ["old-delivered"],
        )

    def test_limit_is_respected(self):
        with sqlite3.connect(server.DB_PATH) as conn:
            for index in range(10):
                insert_event(conn, "ev-%02d" % index, self.now - index)

        data = self.get_history(limit=3)
        self.assertEqual(data["count"], 3)
        self.assertEqual(
            [e["event_id"] for e in data["events"]],
            ["ev-00", "ev-01", "ev-02"],
        )

    def test_delivery_for_other_consumer_does_not_count(self):
        with sqlite3.connect(server.DB_PATH) as conn:
            insert_event(conn, "other-consumer", self.now - 100)

        self.deliver("other-consumer", consumer_id="android")

        data = self.get_history(consumer_id="iphone")
        events = {e["event_id"]: e for e in data["events"]}
        self.assertFalse(events["other-consumer"]["delivered"])


if __name__ == "__main__":
    unittest.main()
