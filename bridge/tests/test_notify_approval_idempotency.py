import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))
import server


class NotifyApprovalIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_data_dir = server.DATA_DIR
        self.original_db_path = server.DB_PATH
        self.original_token = server.TOKEN
        server.DATA_DIR = Path(self.temp_dir.name)
        server.DB_PATH = server.DATA_DIR / 'events.db'
        server.TOKEN = 'test-token-with-at-least-32-characters'
        server.init_db()
        self.approval = {'thread_id': 'thread-test', 'turn_id': 'turn-1', 'kind': 'command', 'command': 'git status', 'cwd': '/tmp/project'}

    def tearDown(self):
        server.DATA_DIR = self.original_data_dir
        server.DB_PATH = self.original_db_path
        server.TOKEN = self.original_token
        self.temp_dir.cleanup()

    def test_duplicate_notification_does_not_reopen_resolved_approval(self):
        result = server.store_notify_approval(self.approval)
        server.resolve_thread_approvals(self.approval['thread_id'])
        server.store_notify_approval(self.approval)
        with sqlite3.connect(server.DB_PATH) as conn:
            status = conn.execute('SELECT status FROM approvals WHERE approval_id=?', (result['approval_id'],)).fetchone()[0]
        self.assertEqual(status, 'resolved')

    def test_pending_approval_remains_visible_after_one_hour(self):
        result = server.store_notify_approval(self.approval)
        with sqlite3.connect(server.DB_PATH) as conn:
            expires_at = conn.execute('SELECT expires_at FROM events WHERE event_id=?', (result['event_id'],)).fetchone()[0]
        self.assertIsNone(expires_at)

    def test_duplicate_notification_does_not_change_creation_time(self):
        result = server.store_notify_approval(self.approval)
        with sqlite3.connect(server.DB_PATH) as conn:
            conn.execute('UPDATE approvals SET created_at=? WHERE approval_id=?', (12345, result['approval_id']))
        server.store_notify_approval(self.approval)
        with sqlite3.connect(server.DB_PATH) as conn:
            created_at = conn.execute('SELECT created_at FROM approvals WHERE approval_id=?', (result['approval_id'],)).fetchone()[0]
        self.assertEqual(created_at, 12345)


class NotificationOnlyDecisionTest(NotifyApprovalIdempotencyTest):
    def test_notification_only_decision_cannot_be_sent_as_daemon_response(self):
        import daemon_client
        result = server.store_notify_approval(self.approval)
        client = daemon_client.DaemonClient(server.DB_PATH)

        class FakeSocket:
            closed = False
            def respond(self, request_id, response):
                raise AssertionError('notification-only approval must not call daemon.respond')

        client.client = FakeSocket()
        decision = client.handle_decision(result['approval_id'], 'accept')
        self.assertFalse(decision['ok'])
        self.assertIn('notification-only', decision['error'])


if __name__ == '__main__':
    unittest.main()
