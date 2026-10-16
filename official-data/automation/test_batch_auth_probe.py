"""Real backend rejection proves the auth probe never selects or completes work."""
import contextlib
from email.message import Message
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import verify_batch_auth as probe

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from service import App, RequestStore


URL = 'https://batch.example.invalid'
SECRET = 'fixture-only-probe-credential'


class BackendOpener:
    def __init__(self, app, scheme='https'):
        self.app, self.scheme, self.calls = app, scheme, []

    def open(self, req, timeout):
        self.calls.append(req.full_url)
        assert req.data == b'{}' and timeout == 8
        statuses, headers = [], []
        env = {'PATH_INFO': req.full_url.removeprefix(URL), 'REQUEST_METHOD': req.method,
               'CONTENT_TYPE': req.get_header('Content-type'), 'CONTENT_LENGTH': str(len(req.data)),
               'wsgi.input': io.BytesIO(req.data), 'wsgi.url_scheme': self.scheme,
               'HTTP_AUTHORIZATION': req.get_header('Authorization'), 'REMOTE_ADDR': '127.0.0.1'}
        body = b''.join(self.app(env, lambda s, h: (statuses.append(s), headers.extend(h))))
        message = Message()
        for key, value in headers:
            message[key] = value
        raise HTTPError(req.full_url, int(statuses[0][:3]), 'fixture', message, io.BytesIO(body))


class BatchAuthProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture.sqlite'
        self.store = RequestStore(str(self.path), b'fixture-ip', clock=lambda: 1000)
        _, _, self.receipt = self.store.submit_pending(250988)
        self.app = App(self.store, batch_mode=True, batch_secret=SECRET)

    def assert_rejection_without_mutation(self, opener, secret, code=None):
        before = self.path.read_bytes()
        with patch.object(self.store, 'snapshot', side_effect=AssertionError('snapshot forbidden')) as snapshot, \
             patch.object(self.store, 'complete', side_effect=AssertionError('ACK forbidden')) as complete, \
             patch.object(self.store, 'charge', side_effect=AssertionError('quota mutation forbidden')) as charge:
            if code:
                with self.assertRaises(probe.AuthProbeFailure) as failure:
                    probe.verify(URL, 'batch.example.invalid', secret, opener=opener)
                self.assertEqual(str(failure.exception), code)
            else:
                self.assertTrue(probe.verify(URL, 'batch.example.invalid', secret, opener=opener))
            snapshot.assert_not_called()
            complete.assert_not_called()
            charge.assert_not_called()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.store.receipt_status(250988, self.receipt), 'pending')
        self.assertEqual(opener.calls, [URL + '/internal/series-batch/complete'])

    def test_valid_auth_is_verified_without_snapshot_ack_or_any_db_write(self):
        self.assert_rejection_without_mutation(BackendOpener(self.app), SECRET)

    def test_wrong_secret_is_auth_rejection_and_preserves_original_work(self):
        self.assert_rejection_without_mutation(BackendOpener(self.app), 'wrong-fixture', 'AUTH_REJECTED')

    def test_wsgi_http_scheme_is_distinguished_without_relaxing_https(self):
        self.assert_rejection_without_mutation(BackendOpener(self.app, scheme='http'), SECRET, 'HTTPS_REJECTED')

    def test_legacy_mode_is_not_mistaken_for_valid_auth(self):
        app = App(self.store, batch_mode=False)
        self.assert_rejection_without_mutation(BackendOpener(app), SECRET, 'MODE_OR_ROUTE_REJECTED')

    def test_config_newline_is_rejected_before_network_and_not_normalized(self):
        opener = Mock()
        with self.assertRaises(probe.AuthProbeFailure) as failure:
            probe.verify(URL, 'batch.example.invalid', SECRET + '\n', opener=opener)
        self.assertEqual(str(failure.exception), 'CONFIG_REJECTED')
        opener.open.assert_not_called()

    def test_unexpected_replies_cannot_report_success_or_disclose_body(self):
        for raw, content_type, returned_url in [
                (b'{"error":"fixture-sensitive-value"}', 'application/json', URL + '/internal/series-batch/complete'),
                (b'{"error":"invalid_completion_request","extra":1}', 'application/json', URL + '/internal/series-batch/complete'),
                (b'{"error":"invalid_completion_request"}', 'text/plain', URL + '/internal/series-batch/complete'),
                (b'x'*2049, 'application/json', URL + '/internal/series-batch/complete'),
                (b'{}', 'application/json', 'https://evil.example.invalid')]:
            headers = Message(); headers['Content-Type'] = content_type
            opener = Mock()
            opener.open.side_effect = HTTPError(returned_url, 400, 'fixture-sensitive-value', headers, io.BytesIO(raw))
            with self.assertRaises(probe.AuthProbeFailure) as failure:
                probe.verify(URL, 'batch.example.invalid', SECRET, opener=opener)
            self.assertNotIn('sensitive', str(failure.exception))
            opener.open.assert_called_once()

    def test_redirect_or_transport_failure_is_sanitized_and_never_retried(self):
        for error, code in [(probe.BatchFailure('fixture-sensitive-value'), 'REDIRECT_OR_BOUNDARY_REJECTED'),
                            (OSError('fixture-sensitive-value'), 'TRANSPORT_FAILURE')]:
            opener = Mock(); opener.open.side_effect = error
            with self.assertRaises(probe.AuthProbeFailure) as failure:
                probe.verify(URL, 'batch.example.invalid', SECRET, opener=opener)
            self.assertEqual(str(failure.exception), code)
            opener.open.assert_called_once()

    def test_2xx_response_is_rejected_instead_of_claiming_safe_auth_success(self):
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=Mock())
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with self.assertRaises(probe.AuthProbeFailure) as failure:
            probe.verify(URL, 'batch.example.invalid', SECRET, opener=opener)
        self.assertEqual(str(failure.exception), 'UNEXPECTED_ACCEPTANCE')
        opener.open.assert_called_once()

    def test_error_response_read_failure_cannot_leak_exception_text(self):
        headers = Message(); headers['Content-Type'] = 'application/json'
        body = Mock(); body.read.side_effect = OSError('fixture-sensitive-value')
        opener = Mock()
        opener.open.side_effect = HTTPError(URL + '/internal/series-batch/complete', 400,
                                           'fixture-sensitive-value', headers, body)
        with self.assertRaises(probe.AuthProbeFailure) as failure:
            probe.verify(URL, 'batch.example.invalid', SECRET, opener=opener)
        self.assertEqual(str(failure.exception), 'TRANSPORT_FAILURE')
        opener.open.assert_called_once()

    def test_cli_reports_only_fixed_diagnostic_codes(self):
        for failure in (None, probe.AuthProbeFailure('AUTH_REJECTED')):
            output = io.StringIO()
            with patch.object(probe, 'verify', side_effect=failure, return_value=True), \
                 contextlib.redirect_stdout(output):
                result = probe.main()
            payload = json.loads(output.getvalue())
            self.assertEqual(result, 1 if failure else 0)
            self.assertEqual(payload['authenticated'], failure is None)
            self.assertNotIn(SECRET, output.getvalue())


if __name__ == '__main__':
    unittest.main()
