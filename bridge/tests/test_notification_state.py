import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class NotificationStateTest(unittest.TestCase):
    def setUp(self):
        with server.STATE_LOCK:
            server.notifications.clear()
            for item in server.pending_completions.values():
                item["timer"].cancel()
            server.pending_completions.clear()

    def tearDown(self):
        self.setUp()

    def test_completion_is_delayed_and_cancelled_by_next_turn(self):
        with patch.object(server, "NOTIFY_PUSH_DELAY", 60):
            server.schedule_completion({
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "cwd": "/tmp/project",
                "message": "done",
            })
            self.assertIn("thread-1", server.pending_completions)
            server.cancel_completion("thread-1")
            self.assertNotIn("thread-1", server.pending_completions)
            self.assertEqual(server.notification_list(10), [])

    def test_completion_publishes_after_delay(self):
        with patch.object(server, "NOTIFY_PUSH_DELAY", 60):
            result = server.schedule_completion({
                "thread_id": "thread-2",
                "turn_id": "turn-1",
                "message": "done",
            })
            pending = server.pending_completions["thread-2"]
            pending["timer"].cancel()
            server._publish_completion("thread-2", "turn-1")
            self.assertEqual(server.notification_list(10)[0]["event_id"], result["event_id"])

    def test_ack_removes_notification(self):
        item = {"event_id": "event-1", "type": "task_completed"}
        server.queue_notification(item)
        self.assertTrue(server.acknowledge_notification("event-1"))
        self.assertFalse(server.acknowledge_notification("event-1"))

    def test_duplicate_notification_is_ignored(self):
        item = {"event_id": "event-2", "type": "task_completed"}
        self.assertTrue(server.queue_notification(item))
        self.assertFalse(server.queue_notification(item))
        self.assertEqual(len(server.notification_list(10)), 1)


if __name__ == "__main__":
    unittest.main()
