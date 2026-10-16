import io
import json
import os
import tempfile
import threading
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from service import (App, BATCH_INTERVAL_SECONDS, COMPLETED_RETENTION_SECONDS,
                     PENDING_CAPACITY, RequestStore, from_environment)


class BatchFoundationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [1_000_000.0]
        self.path = str(Path(self.temp.name) / "requests.db")
        self.secret = "synthetic-batch-secret-only-for-tests"
        self.store = self.make_store()
        self.app = App(self.store, batch_mode=True, batch_secret=self.secret)

    def make_store(self):
        return RequestStore(self.path, b"test-only-ip-key", clock=lambda: self.now[0])

    def call(self, path, value=b"{}", *, method="POST", secret=None, https=True, ip="127.0.0.1"):
        raw = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode()
        env = {"PATH_INFO": path, "REQUEST_METHOD": method, "CONTENT_TYPE": "application/json",
               "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw), "REMOTE_ADDR": ip,
               "wsgi.url_scheme": "https" if https else "http"}
        if secret is not None:
            env["HTTP_AUTHORIZATION"] = "Bearer " + secret
        status, headers = [], []
        body = b"".join(self.app(env, lambda code, response_headers: (status.append(code), headers.extend(response_headers))))
        return int(status[0][:3]), json.loads(body), dict(headers)

    def post(self, tmdb_id=123):
        return self.call("/v1/series-requests", {"tmdbId": tmdb_id})

    def snapshot(self):
        status, body, _ = self.call("/internal/series-batch/snapshot", secret=self.secret)
        self.assertEqual(200, status)
        return body["items"]

    def complete(self, *items):
        return self.call("/internal/series-batch/complete", {"items": list(items)}, secret=self.secret)

    def test_mode_accepts_durable_request_without_dispatcher_and_returns_hex_receipt(self):
        status, body, _ = self.post(123)
        self.assertEqual(202, status)
        self.assertEqual({"accepted", "tmdbId", "state", "requestReceipt"}, set(body))
        self.assertEqual("queued", body["state"])
        self.assertRegex(body["requestReceipt"], r"^[0-9a-f]{64}$")
        self.assertEqual("pending", self.store.receipt_status(123, body["requestReceipt"]))

    def test_pending_table_has_only_the_five_approved_columns(self):
        with self.store._connect() as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(pending_requests)")]
        self.assertEqual(["tmdb_id", "request_receipt", "requested_at", "next_eligible_at", "completed_at"], columns)

    def test_batch_duplicate_rotates_receipt_without_resetting_request_or_deferral(self):
        first = self.post(123)[1]
        item = self.snapshot()[0]
        self.now[0] += 100
        second = self.post(123)[1]
        row = self.store._connect()
        with row as db:
            requested_at, due_at = db.execute(
                "SELECT requested_at,next_eligible_at FROM pending_requests WHERE tmdb_id=123").fetchone()
        self.assertEqual(first["requestReceipt"], item["requestReceipt"])
        self.assertNotEqual(first["requestReceipt"], second["requestReceipt"])
        self.assertEqual(1_000_000.0, requested_at)
        self.assertEqual(1_000_000.0 + BATCH_INTERVAL_SECONDS, due_at)
        self.assertEqual("unknown", self.store.receipt_status(123, first["requestReceipt"]))
        self.assertEqual("pending", self.store.receipt_status(123, second["requestReceipt"]))

    def test_old_ack_cannot_complete_newer_receipt_then_new_receipt_can_complete(self):
        first = self.post(123)[1]["requestReceipt"]
        old_item = self.snapshot()[0]
        second = self.post(123)[1]["requestReceipt"]
        self.assertEqual("stale", self.complete(old_item)[1]["items"][0]["state"])
        self.assertEqual("pending", self.store.receipt_status(123, second))
        self.now[0] += BATCH_INTERVAL_SECONDS
        current_item = self.snapshot()[0]
        self.assertEqual(second, current_item["requestReceipt"])
        self.assertEqual("complete", self.complete(current_item)[1]["items"][0]["state"])
        self.assertEqual("complete", self.store.receipt_status(123, second))
        self.assertEqual("unknown", self.store.receipt_status(123, first))

    def test_ack_then_new_post_creates_new_pending_obligation(self):
        old = self.post(88)[1]["requestReceipt"]
        item = self.snapshot()[0]
        self.assertEqual("complete", self.complete(item)[1]["items"][0]["state"])
        new = self.post(88)[1]["requestReceipt"]
        self.assertNotEqual(old, new)
        self.assertEqual("pending", self.store.receipt_status(88, new))
        self.assertEqual("unknown", self.store.receipt_status(88, old))

    def test_duplicate_ack_is_idempotent(self):
        self.post(9)
        item = self.snapshot()[0]
        self.assertEqual("complete", self.complete(item)[1]["items"][0]["state"])
        self.assertEqual("already_complete", self.complete(item)[1]["items"][0]["state"])

    def test_snapshot_is_bounded_ordered_and_not_a_claim(self):
        for value in (3, 1, 2, 4):
            self.post(value)
        items = self.snapshot()
        self.assertEqual([1, 2, 3], [item["tmdbId"] for item in items])
        # All selected rows remain pending after a simulated crash/restart.
        self.store = self.make_store()
        self.assertEqual(["pending"] * 3, [self.store.receipt_status(i, self._receipt(i)) for i in (1, 2, 3)])
        self.assertEqual([4], [item["tmdbId"] for item in self.snapshot()])
        self.now[0] += BATCH_INTERVAL_SECONDS
        self.assertEqual([1, 2, 3], [item["tmdbId"] for item in self.snapshot()])

    def _receipt(self, tmdb_id):
        with self.store._connect() as db:
            return db.execute("SELECT request_receipt FROM pending_requests WHERE tmdb_id=?", (tmdb_id,)).fetchone()[0]

    def test_concurrent_snapshots_do_not_duplicate_due_rows_and_never_exceed_three(self):
        for value in range(1, 8):
            self.post(value)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.snapshot())) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        flat = [item["tmdbId"] for batch in results for item in batch]
        self.assertEqual(6, len(flat))
        self.assertEqual(6, len(set(flat)))
        self.assertTrue(all(len(batch) <= 3 for batch in results))
        self.assertEqual([7], [item["tmdbId"] for item in self.snapshot()])

    def test_invalid_proof_is_rejected_atomically_and_proof_binds_id_and_receipt(self):
        self.post(123)
        item = self.snapshot()[0]
        bad = dict(item, ackProof="0" * 64)
        self.assertEqual(401, self.complete(bad)[0])
        bad_id = dict(item, tmdbId=124)
        self.assertEqual(401, self.complete(bad_id)[0])
        self.assertEqual("pending", self.store.receipt_status(123, item["requestReceipt"]))

    def test_internal_auth_https_method_and_exact_schema(self):
        path = "/internal/series-batch/snapshot"
        self.assertEqual(401, self.call(path, secret="wrong")[0])
        self.assertEqual(400, self.call(path, secret=self.secret, https=False)[0])
        self.assertEqual(405, self.call(path, secret=self.secret, method="GET")[0])
        self.assertEqual(400, self.call(path, {"limit": 1000}, secret=self.secret)[0])
        self.assertEqual(400, self.call(path, b'{"x":1,"x":2}', secret=self.secret)[0])
        self.assertEqual("no-store", self.call(path, secret=self.secret)[2]["Cache-Control"])

    def test_completion_list_strictly_validates_all_items_before_mutating(self):
        self.post(1)
        first = self.snapshot()[0]
        invalid = {"tmdbId": 2, "requestReceipt": first["requestReceipt"], "ackProof": first["ackProof"], "extra": True}
        self.assertEqual(400, self.call("/internal/series-batch/complete", {"items": [first, invalid]}, secret=self.secret)[0])
        self.assertEqual("pending", self.store.receipt_status(1, first["requestReceipt"]))

    def test_status_is_exact_receipt_non_enumerating_and_rate_limited(self):
        receipt = self.post(8)[1]["requestReceipt"]
        status, body, headers = self.call("/v1/series-requests/status", {"tmdbId": 8, "requestReceipt": receipt})
        self.assertEqual((200, {"tmdbId": 8, "state": "pending"}), (status, body))
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual("unknown", self.call("/v1/series-requests/status",
            {"tmdbId": 8, "requestReceipt": "0" * 64})[1]["state"])
        self.assertEqual(400, self.call("/v1/series-requests/status",
            {"tmdbId": 8, "requestReceipt": receipt, "other": True})[0])

    def test_status_has_existing_metadata_rate_limit_without_charging_admission(self):
        self.store.tmdb_per_client = 1
        self.post(1)
        self.assertEqual(200, self.call("/v1/series-requests/status",
            {"tmdbId": 1, "requestReceipt": self._receipt(1)})[0])
        self.assertEqual(429, self.call("/v1/series-requests/status",
            {"tmdbId": 1, "requestReceipt": self._receipt(1)})[0])

    def test_pending_capacity_rejects_without_eviction_and_completed_rows_do_not_count(self):
        with self.store._connect() as db:
            db.executemany("INSERT INTO pending_requests VALUES (?, ?, ?, ?, NULL)",
                           ((i, f"{i:064x}", self.now[0], self.now[0],) for i in range(1, PENDING_CAPACITY + 1)))
        status, body, headers = self.post(PENDING_CAPACITY + 1)
        self.assertEqual(503, status)
        self.assertEqual("queue_full", body["error"])
        self.assertEqual(str(BATCH_INTERVAL_SECONDS), headers["Retry-After"])
        with self.store._connect() as db:
            self.assertEqual(PENDING_CAPACITY, db.execute("SELECT COUNT(*) FROM pending_requests WHERE completed_at IS NULL").fetchone()[0])
            db.execute("UPDATE pending_requests SET completed_at=? WHERE tmdb_id=1", (self.now[0],))
        self.assertEqual(202, self.post(PENDING_CAPACITY + 1)[0])

    def test_pending_never_expires_and_completed_receipts_expire_without_resurrection(self):
        pending = self.post(1)[1]["requestReceipt"]
        completed = self.post(2)[1]["requestReceipt"]
        item = next(item for item in self.snapshot() if item["tmdbId"] == 2)
        self.complete(item)
        self.now[0] += COMPLETED_RETENTION_SECONDS + 1
        self.assertEqual("pending", self.store.receipt_status(1, pending))
        self.assertEqual("unknown", self.store.receipt_status(2, completed))
        self.assertEqual([1], [item["tmdbId"] for item in self.store.snapshot(self.secret.encode())])

    def test_legacy_import_is_explicit_idempotent_conservative_and_preserves_old_rows(self):
        with self.store._connect() as db:
            db.execute("INSERT INTO requests VALUES (10, ?)", (self.now[0] - 100,))
            db.execute("INSERT INTO requests VALUES (11, ?)", (self.now[0] - 90_000,))
            db.execute("INSERT INTO rate_events VALUES ('hashed-only', ?)", (self.now[0],))
        with self.store._connect() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM pending_requests").fetchone()[0])
        self.assertEqual(1, self.store.import_legacy_requests())
        self.assertEqual(0, self.store.import_legacy_requests())
        with self.store._connect() as db:
            self.assertEqual(2, db.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM rate_events").fetchone()[0])
            legacy_receipt = db.execute("SELECT request_receipt FROM pending_requests WHERE tmdb_id=10").fetchone()[0]
        self.now[0] += BATCH_INTERVAL_SECONDS
        item = self.snapshot()[0]
        self.assertEqual(10, item["tmdbId"])
        self.complete(item)
        self.now[0] += COMPLETED_RETENTION_SECONDS + 1
        self.assertEqual(0, self.store.import_legacy_requests())
        self.assertEqual("unknown", self.store.receipt_status(10, legacy_receipt))

    def test_batch_environment_mode_does_not_require_github_pat(self):
        env = {"SERIES_REQUEST_MODE": "batch", "SERIES_BATCH_API_SECRET": self.secret,
               "REQUEST_DB_PATH": self.path, "CLIENT_IP_HMAC_KEY": "synthetic-key"}
        with patch.dict(os.environ, env, clear=True):
            app = from_environment()
        self.assertTrue(app.batch_mode)
        self.assertIsNone(app.dispatcher)

    def test_batch_mode_requires_secret_and_legacy_mode_stays_unavailable_for_internal_route(self):
        with self.assertRaises(ValueError):
            App(self.store, batch_mode=True)
        legacy = App(self.store, dispatcher=None)
        self.app = legacy
        self.assertEqual(404, self.call("/internal/series-batch/snapshot", secret=self.secret)[0])

    def test_missing_auth_and_oversized_malformed_bodies_are_rejected(self):
        route = '/internal/series-batch/snapshot'
        self.assertEqual(401, self.call(route)[0])
        self.assertEqual(400, self.call(route, b'{', secret=self.secret)[0])
        self.assertEqual(413, self.call(route, b' ' * 5000, secret=self.secret)[0])
        self.assertEqual(400, self.call('/internal/series-batch/complete',
            b'{"items":[],"items":[]}', secret=self.secret)[0])

    def test_wrong_receipt_and_unselected_proof_cannot_complete(self):
        self.post(1); self.post(2)
        selected = self.snapshot()[0]
        wrong = dict(selected, requestReceipt='f' * 64)
        self.assertEqual(401, self.complete(wrong)[0])
        not_selected = dict(selected, tmdbId=3)
        self.assertEqual(401, self.complete(not_selected)[0])

    def test_valid_then_invalid_proof_list_is_atomic(self):
        self.post(1); self.post(2)
        first, second = self.snapshot()
        second['ackProof'] = '0' * 64
        self.assertEqual(401, self.complete(first, second)[0])
        self.assertEqual('pending', self.store.receipt_status(1, first['requestReceipt']))

    def test_restart_between_snapshot_and_ack_preserves_proof_and_completion(self):
        self.post(1); item = self.snapshot()[0]
        self.store = self.make_store()
        self.app = App(self.store, batch_mode=True, batch_secret=self.secret)
        self.assertEqual('complete', self.complete(item)[1]['items'][0]['state'])

    def test_equal_clock_requests_have_distinct_receipts(self):
        first = self.post(1)[1]['requestReceipt']
        second = self.post(1)[1]['requestReceipt']
        self.assertNotEqual(first, second)

    def test_202_observer_sees_committed_pending_and_db_failure_has_no_success(self):
        real_submit = self.store.submit_pending
        def committed(identifier):
            response = real_submit(identifier)
            restarted = self.make_store()
            self.assertEqual('pending', restarted.receipt_status(identifier, response[2]))
            return response
        with patch.object(self.store, 'submit_pending', side_effect=committed):
            self.assertEqual(202, self.post(1)[0])
        with patch.object(self.store, 'submit_pending', side_effect=sqlite3.OperationalError('synthetic commit failure')):
            self.assertEqual(503, self.post(2)[0])
        with self.store._connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM pending_requests WHERE tmdb_id=2').fetchone())

    def test_wrong_status_receipt_and_unknown_id_have_identical_non_enumerating_state(self):
        self.post(1)
        statuses = [self.call('/v1/series-requests/status',
                    {'tmdbId': identifier, 'requestReceipt': '0'*64})[1]['state'] for identifier in (1, 2)]
        self.assertEqual(statuses, ['unknown', 'unknown'])

    def test_constant_time_auth_proof_path_and_pat_reuse_rejected(self):
        from service import hmac
        original = hmac.compare_digest
        self.post(1)
        with patch.object(hmac, 'compare_digest', wraps=original) as comparison:
            item = self.snapshot()[0]
            self.complete(item)
            self.assertGreaterEqual(comparison.call_count, 3)
        with patch.dict(os.environ, {'SERIES_REQUEST_MODE':'batch',
             'SERIES_BATCH_API_SECRET':self.secret, 'GITHUB_TOKEN':self.secret}, clear=True):
            with self.assertRaises(ValueError): from_environment()

    def test_legacy_schema_is_migrated_additively_and_restart_does_not_reset_quotas(self):
        other = str(Path(self.temp.name) / 'legacy.sqlite')
        db = sqlite3.connect(other)
        with db:
            db.execute('CREATE TABLE requests (tmdb_id INTEGER PRIMARY KEY, at REAL NOT NULL)')
            db.execute('CREATE TABLE rate_events (ip_hash TEXT NOT NULL, at REAL NOT NULL)')
            db.execute('CREATE TABLE tmdb_rate_events (ip_hash TEXT NOT NULL, at REAL NOT NULL)')
            db.execute('INSERT INTO requests VALUES (44,?)', (self.now[0],))
            db.execute('INSERT INTO rate_events VALUES (?,?)', ('synthetic-hash', self.now[0]))
        db.close()
        for _ in range(2):
            migrated = RequestStore(other, b'synthetic-ip', clock=lambda:self.now[0])
            with migrated._connect() as check:
                self.assertEqual(1, check.execute('SELECT COUNT(*) FROM requests').fetchone()[0])
                self.assertEqual(1, check.execute('SELECT COUNT(*) FROM rate_events').fetchone()[0])
                self.assertEqual(0, check.execute('SELECT COUNT(*) FROM pending_requests').fetchone()[0])


if __name__ == "__main__":
    unittest.main()
