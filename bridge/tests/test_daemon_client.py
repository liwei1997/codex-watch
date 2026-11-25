import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

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
