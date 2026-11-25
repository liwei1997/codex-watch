import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import daemon_client
import server


class FakeDaemon:
    def __init__(self, result=None):
        self.result = result or {'ok': True, 'approval_id': 'approval-1'}
        self.calls = []

    def handle_decision(self, approval_id, decision):
        self.calls.append((approval_id, decision))
        return self.result

    def list_approvals(self, status):
        return [{'approval_id': 'approval-1', 'status': status}]


class BridgeHttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_token = server.TOKEN
        server.TOKEN = 'test-token-' + ('x' * 32)
        cls.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = 'http://127.0.0.1:' + str(cls.httpd.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        server.TOKEN = cls.original_token

    def setUp(self):
        self.original_daemon = daemon_client.INSTANCE
        self.fake_daemon = FakeDaemon()
        daemon_client.INSTANCE = self.fake_daemon
        with server.STATE_LOCK:
            server.notifications.clear()

    def tearDown(self):
        daemon_client.INSTANCE = self.original_daemon
        with server.STATE_LOCK:
            server.notifications.clear()

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                'Authorization': 'Bearer ' + server.TOKEN,
                'Content-Type': 'application/json',
            },
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode('utf-8'))

    def test_notification_poll_and_ack_routes(self):
        item = {'event_id': 'event-1', 'type': 'task_completed'}
        server.queue_notification(item)

        status, result = self.request('GET', '/v1/notifications?limit=20')
        self.assertEqual(status, 200)
        self.assertEqual(result['notifications'], [item])

        status, result = self.request(
            'POST', '/v1/notifications/ack', {'event_id': 'event-1'}
        )
        self.assertEqual(status, 200)
        self.assertTrue(result['removed'])
        self.assertEqual(server.notification_list(20), [])

    def test_legacy_completion_and_thread_observe_routes_are_removed(self):
        status, _ = self.request(
            'POST', '/v1/completions',
            {'thread_id': 'thread-1', 'turn_id': 'turn-1'},
        )
        self.assertEqual(status, 404)

        status, _ = self.request(
            'POST', '/v1/threads/observe',
            {'thread_id': 'thread-1', 'cwd': '/workspace/project'},
        )
        self.assertEqual(status, 404)

    def test_approval_decision_route_dispatches_to_daemon(self):
        status, result = self.request(
            'POST',
            '/v1/approvals/approval-1/decision',
            {'decision': 'accept'},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result['ok'])
        self.assertEqual(self.fake_daemon.calls, [('approval-1', 'accept')])

    def test_approval_decision_route_returns_conflict(self):
        self.fake_daemon.result = {'ok': False, 'error': 'approval already resolved'}
        status, result = self.request(
            'POST',
            '/v1/approvals/approval-1/decision',
            {'decision': 'decline'},
        )
        self.assertEqual(status, 409)
        self.assertFalse(result['ok'])

    def test_approval_decision_route_requires_daemon(self):
        daemon_client.INSTANCE = None
        status, result = self.request(
            'POST',
            '/v1/approvals/approval-1/decision',
            {'decision': 'accept'},
        )
        self.assertEqual(status, 503)
        self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
