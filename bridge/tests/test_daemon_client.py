import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon_client import DaemonClient


class FakeConnection:
    def __init__(self, error=None):
        self.closed = False
        self.error = error
        self.responses = []

    def respond(self, request_id, response):
        if self.error:
            raise self.error
        self.responses.append((request_id, response))


class DaemonCompletionNotificationTest(unittest.TestCase):
    def test_turn_completed_schedules_notification_from_app_server_event(self):
        sink = Mock(return_value={"ok": True, "event_id": "event-1"})
        client = DaemonClient(turn_completed_sink=sink)
        thread_id = "thread-1"
        client.threads[thread_id] = {"cwd": "/workspace/project", "name": "project"}

        client._on_notification("turn/completed", {
            "threadId": thread_id,
            "turn": {
                "id": "turn-1",
                "items": [
                    {"type": "reasoning", "text": "not sent"},
                    {"type": "agentMessage", "phase": "final_answer", "text": "Task finished."},
                ],
            },
        }, {})

        sink.assert_called_once_with({
            "thread_id": thread_id,
            "turn_id": "turn-1",
            "cwd": "/workspace/project",
            "message": "Task finished.",
        })

    def test_turn_completed_without_ids_does_not_schedule(self):
        sink = Mock()
        client = DaemonClient(turn_completed_sink=sink)

        client._on_notification("turn/completed", {
            "turn": {"items": []},
        }, {})

        sink.assert_not_called()


class DaemonStatusRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.sink = Mock()
        self.cancel = Mock()
        self.client = DaemonClient(turn_completed_sink=self.sink, turn_started_sink=self.cancel)
        self.connection = Mock()
        self.client.client = self.connection
        self.turn = {"id": "turn-1", "status": "completed", "completedAt": 101,
                     "items": [{"type": "agentMessage", "phase": "final_answer", "text": "hello"}]}
        self.connection.request.return_value = {"data": [self.turn]}

    def status(self, status, at=100000):
        self.client._on_notification("thread/status/changed", {
            "threadId": "thread-1", "status": {"type": status}
        }, {"emittedAtMs": at})

    def test_active_idle_without_turn_events(self):
        with patch.object(self.client, "_join_thread"), patch("daemon_client.threading.Thread") as worker:
            self.status("active")
            self.status("idle")
            args = worker.call_args.kwargs["args"]
        self.client._recover_completion(*args)
        self.connection.request.assert_called_once_with("thread/turns/list", {
            "threadId": "thread-1", "limit": 1, "sortDirection": "desc", "itemsView": "full"
        }, timeout=10)
        self.sink.assert_called_once_with({
            "thread_id": "thread-1", "turn_id": "turn-1", "cwd": "", "message": "hello"
        })
        self.client._on_notification("turn/completed", {"threadId": "thread-1", "turn": self.turn}, {})
        self.sink.assert_called_once()

    def test_new_activity_invalidates_old_lookup(self):
        with patch.object(self.client, "_join_thread"), patch("daemon_client.threading.Thread") as worker:
            self.status("active")
            self.status("idle")
            args = worker.call_args.kwargs["args"]
            self.status("active", 102000)
        self.client._recover_completion(*args)
        self.connection.request.assert_not_called()
        self.sink.assert_not_called()
        self.assertEqual(self.cancel.call_count, 2)

    def test_idle_without_observed_activity_ignores_history(self):
        with patch("daemon_client.threading.Thread") as worker:
            self.status("idle")
        worker.assert_not_called()
        self.sink.assert_not_called()

    def test_stale_turn_is_not_notified(self):
        with patch.object(self.client, "_join_thread"), patch("daemon_client.threading.Thread") as worker:
            self.status("active", 102000)
            self.status("idle")
            args = worker.call_args.kwargs["args"]
        with patch.object(self.client.stop_event, "wait", return_value=False):
            self.client._recover_completion(*args)
        self.sink.assert_not_called()

    def test_loaded_thread_requires_actual_subscription(self):
        self.connection.request.return_value = {"thread": {"cwd": "/tmp/project"}}
        self.client._ensure_joined("thread-1", self.connection)
        self.connection.request.assert_called_once_with("thread/resume", {
            "threadId": "thread-1", "excludeTurns": True
        }, timeout=20)
        self.assertIn("thread-1", self.client.joined)


class DaemonDecisionTest(unittest.TestCase):
    def setUp(self):
        self.client = DaemonClient()
        self.connection = FakeConnection()
        self.client.client = self.connection
        self.timer = Mock()
        self.client.approvals['approval-1'] = {
            'approval_id': 'approval-1',
            'request_id': 42,
            'status': 'pending',
            'timer': self.timer,
        }

    def test_handle_decision_responds_to_original_request(self):
        result = self.client.handle_decision('approval-1', 'accept')

        self.assertTrue(result['ok'])
        self.assertEqual(self.connection.responses, [(42, {'decision': 'accept'})])
        self.assertEqual(self.client.approvals['approval-1']['status'], 'sent')
        self.assertEqual(self.client.approvals['approval-1']['decision'], 'accept')
        self.timer.cancel.assert_called_once()

    def test_handle_decision_rejects_invalid_decision(self):
        result = self.client.handle_decision('approval-1', 'maybe')

        self.assertFalse(result['ok'])
        self.assertEqual(self.connection.responses, [])
        self.assertEqual(self.client.approvals['approval-1']['status'], 'pending')

    def test_handle_decision_rejects_missing_approval(self):
        result = self.client.handle_decision('missing', 'accept')

        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'approval not found')

    def test_handle_decision_restores_pending_after_transport_error(self):
        self.connection.error = RuntimeError('socket closed')
        result = self.client.handle_decision('approval-1', 'decline')

        self.assertFalse(result['ok'])
        self.assertEqual(self.client.approvals['approval-1']['status'], 'pending')
        self.timer.cancel.assert_not_called()

    def test_handle_decision_rejects_disconnected_daemon(self):
        self.client.client = None
        result = self.client.handle_decision('approval-1', 'accept')

        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'not connected to codex daemon')


if __name__ == '__main__':
    unittest.main()
