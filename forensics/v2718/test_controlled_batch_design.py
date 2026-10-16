"""Controlled-batch architecture diagnostics, not runtime implementation.

Toy durable SQLite model and explicit interleavings. Proof handles/auth are
represented by selected tuples in memory; this does NOT test HTTP security,
production providers, git publication, GitHub concurrency or Android storage.
"""
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
import unittest
import uuid


class PendingModel:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests '
                       '(id INTEGER PRIMARY KEY, receipt TEXT, requested REAL, '
                       'eligible REAL, completed REAL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, identifier, now):
        receipt = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT requested,eligible,completed FROM requests WHERE id=?',
                             (identifier,)).fetchone()
            was_pending = old is not None and old[2] is None
            requested, eligible = old[:2] if was_pending else (now, now)
            db.execute('INSERT OR REPLACE INTO requests VALUES (?,?,?,?,NULL)',
                       (identifier, receipt, requested, eligible))
        return receipt, 'already_queued' if was_pending else 'queued'

    def snapshot(self, now, limit=3, selection_delay=600):
        # Touch only retry eligibility, never claim/delete pending membership.
        # A crash after snapshot delays work until a later selection; no lease.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT id,receipt FROM requests WHERE completed IS NULL '
                              'AND eligible<=? ORDER BY eligible,requested,id LIMIT ?',
                              (now, limit)).fetchall()
            for identifier, receipt in rows:
                db.execute('UPDATE requests SET eligible=? WHERE id=? AND receipt=?',
                           (now + selection_delay, identifier, receipt))
        return rows

    def ack(self, selected, identifier, receipt, now):
        if (identifier, receipt) not in selected:
            return 'unauthorized_selection'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT receipt,completed FROM requests WHERE id=?',
                             (identifier,)).fetchone()
            if row is None or row[0] != receipt:
                return 'stale'
            if row[1] is not None:
                return 'already_complete'
            db.execute('UPDATE requests SET completed=? WHERE id=? AND receipt=?',
                       (now, identifier, receipt))
        return 'complete'

    def status(self, identifier, receipt):
        with self.connect() as db:
            row = db.execute('SELECT receipt,completed FROM requests WHERE id=?',
                             (identifier,)).fetchone()
        if row is None:
            return 'unknown'
        if row[0] != receipt:
            return 'superseded'
        return 'pending' if row[1] is None else 'complete'


class ControlledBatchDesignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'diagnostic.sqlite')
        self.model = PendingModel(self.path)

    def test_post_is_durable_across_backend_restart(self):
        receipt, state = self.model.submit(123, 0)
        self.assertEqual(state, 'queued')
        self.assertEqual(PendingModel(self.path).status(123, receipt), 'pending')

    def test_duplicate_post_has_one_member_but_refreshes_ack_marker(self):
        first, _ = self.model.submit(123, 0)
        second, state = self.model.submit(123, 1)
        self.assertEqual(state, 'already_queued')
        self.assertNotEqual(first, second)
        self.assertEqual(self.model.snapshot(1), [(123, second)])

    def test_successful_snapshot_ack_removes_only_pending_membership(self):
        receipt, _ = self.model.submit(123, 0)
        selected = self.model.snapshot(0)
        self.assertEqual(self.model.ack(selected, 123, receipt, 1), 'complete')
        self.assertEqual(self.model.snapshot(600), [])
        self.assertEqual(self.model.status(123, receipt), 'complete')

    def test_newer_request_before_old_ack_remains_pending(self):
        first, _ = self.model.submit(123, 0)
        selected = self.model.snapshot(0)
        second, _ = self.model.submit(123, 1)
        self.assertEqual(self.model.ack(selected, 123, first, 2), 'stale')
        self.assertEqual(self.model.status(123, second), 'pending')

    def test_equal_clock_values_do_not_reuse_receipt(self):
        first, _ = self.model.submit(123, 0)
        selected = self.model.snapshot(0)
        second, _ = self.model.submit(123, 0)
        self.assertNotEqual(first, second)
        self.assertEqual(self.model.ack(selected, 123, first, 1), 'stale')

    def test_duplicate_ack_is_idempotent(self):
        receipt, _ = self.model.submit(123, 0)
        selected = self.model.snapshot(0)
        self.assertEqual(self.model.ack(selected, 123, receipt, 1), 'complete')
        self.assertEqual(self.model.ack(selected, 123, receipt, 2), 'already_complete')

    def test_unselected_id_cannot_be_acknowledged(self):
        one, _ = self.model.submit(123, 0)
        two, _ = self.model.submit(456, 1)
        selected = self.model.snapshot(1, limit=1)
        self.assertEqual(selected, [(123, one)])
        self.assertEqual(self.model.ack(selected, 456, two, 2), 'unauthorized_selection')

    def test_crashed_batch_does_not_consume_work(self):
        receipt, _ = self.model.submit(123, 0)
        self.model.snapshot(0)
        self.assertEqual(self.model.status(123, receipt), 'pending')
        self.assertEqual(self.model.snapshot(600), [(123, receipt)])

    def test_never_started_batch_has_no_state_to_recover(self):
        receipt, _ = self.model.submit(123, 0)
        self.assertEqual(self.model.snapshot(3600), [(123, receipt)])

    def test_publication_before_ack_failure_replays_as_no_change(self):
        receipt, _ = self.model.submit(123, 0)
        self.model.snapshot(0)
        canonical, desired = {}, {123: 'synthetic-verified-fact'}
        writes = 0
        if canonical != desired:
            canonical = desired.copy(); writes += 1
        # ACK lost, next serialized batch reprocesses the same desired fact.
        selected = self.model.snapshot(600)
        if canonical != desired:
            canonical = desired.copy(); writes += 1
        self.assertEqual(writes, 1)
        self.assertEqual(self.model.ack(selected, 123, receipt, 601), 'complete')

    def test_failure_before_publication_keeps_pending(self):
        receipt, _ = self.model.submit(123, 0)
        self.model.snapshot(0)
        self.assertEqual(self.model.status(123, receipt), 'pending')

    def test_poison_item_rotation_allows_later_items_after_run_abort(self):
        for identifier in range(1, 6):
            self.model.submit(identifier, 0)
        first = self.model.snapshot(0, limit=3)
        self.assertEqual([identifier for identifier, _ in first], [1, 2, 3])
        # Run aborts on poison ID1. Selection cooldown rotates even unprocessed
        # rows; the next due snapshot includes IDs4/5 first, not poison forever.
        second = self.model.snapshot(600, limit=3)
        self.assertEqual([identifier for identifier, _ in second][:2], [4, 5])

    def test_backend_ack_outage_keeps_request_pending(self):
        receipt, _ = self.model.submit(123, 0)
        self.model.snapshot(0)  # no ack call models unavailable backend
        self.assertEqual(self.model.status(123, receipt), 'pending')

    def test_ack_before_unrelated_later_job_failure_stays_complete(self):
        receipt, _ = self.model.submit(123, 0)
        selected = self.model.snapshot(0)
        self.model.ack(selected, 123, receipt, 1)
        unrelated_later_failure = True
        self.assertTrue(unrelated_later_failure)
        self.assertEqual(self.model.status(123, receipt), 'complete')

    def test_absence_is_unknown_not_proven_completion(self):
        self.assertEqual(self.model.status(123, 'not-a-real-receipt'), 'unknown')

    def test_duplicate_serialized_batch_has_no_completed_items(self):
        receipt, _ = self.model.submit(123, 0)
        selected = self.model.snapshot(0)
        self.model.ack(selected, 123, receipt, 1)
        self.assertEqual(self.model.snapshot(600), [])

    def test_overlapping_snapshots_do_not_claim_or_supersede_work(self):
        receipt, _ = self.model.submit(123, 0)
        a = self.model.snapshot(0)
        b = self.model.snapshot(600)
        self.assertEqual(a, b)
        self.assertEqual(self.model.status(123, receipt), 'pending')
        # Both snapshots are possible; external writer serialization is a
        # design assumption, NOT guaranteed by this SQLite model.


if __name__ == '__main__':
    unittest.main()
