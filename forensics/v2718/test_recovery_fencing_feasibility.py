"""Adversarial architecture models; not production regression tests.

Explicit interleaving is deterministic. No HTTP, git, provider, or runtime
implementation is invoked. A successful fence is an observation, not a lock.
"""
import unittest


class World:
    def __init__(self):
        self.current = 'A'
        self.remote_head = 'H0'
        self.writer = 'A'
        self.available = True
        self.trace = []

    def fence(self, attempt):
        ok = self.available and attempt == self.current
        self.trace.append(('check', attempt, ok))
        return ok

    def supersede(self, attempt):
        self.current = attempt
        self.trace.append(('current', attempt))

    def push(self, attempt, base, new_head):
        # Models even stronger exact expected-head compare-and-swap than a
        # normal push. It still has no atomic knowledge of backend generation.
        if self.writer != attempt or self.remote_head != base:
            return False
        self.remote_head = new_head
        self.trace.append(('remote-write', attempt, self.current))
        return True


class RecoveryFencingFeasibilityTests(unittest.TestCase):
    def test_queued_stale_a_is_rejected_by_early_check(self):
        w = World(); w.supersede('B')
        self.assertFalse(w.fence('A'))
        self.assertEqual(w.remote_head, 'H0')

    def test_running_stale_a_is_rejected_if_superseded_before_final_check(self):
        w = World(); self.assertTrue(w.fence('A'))
        local_result = 'computed-A'
        w.supersede('B')
        self.assertFalse(w.fence('A'))
        self.assertEqual(local_result, 'computed-A')
        self.assertEqual(w.remote_head, 'H0')

    def test_start_check_does_not_protect_later_monitored_push(self):
        w = World(); admitted = w.fence('A')
        w.supersede('B')
        self.assertTrue(admitted)
        self.assertTrue(w.push('A', 'H0', 'monitored-A'))
        self.assertEqual(w.trace[-1], ('remote-write', 'A', 'B'))

    def test_latest_pre_push_check_still_has_toctou(self):
        w = World(); base = w.remote_head
        self.assertTrue(w.fence('A'))  # last possible HTTP check
        w.supersede('B')             # no repository update required
        self.assertTrue(w.push('A', base, 'monitored-A'))
        self.assertEqual(w.current, 'B')

    def test_local_commit_can_happen_after_supersession(self):
        w = World(); self.assertTrue(w.fence('A'))
        w.supersede('B')
        local_commit = ('A', 'H0', 'monitored-A')
        self.assertEqual(w.remote_head, 'H0')  # still runner-local
        self.assertTrue(w.push(*local_commit))

    def test_monitored_success_does_not_authorize_later_promotion(self):
        w = World(); self.assertTrue(w.push('A', 'H0', 'monitored-A'))
        w.supersede('B')
        self.assertFalse(w.fence('A'))  # promote requires independent check
        self.assertEqual(w.remote_head, 'monitored-A')

    def test_promotion_fence_has_same_check_to_push_race(self):
        w = World(); w.remote_head = 'monitored-A'
        self.assertTrue(w.fence('A'))
        w.supersede('B')
        self.assertTrue(w.push('A', 'monitored-A', 'production-A'))
        self.assertEqual(w.trace[-1], ('remote-write', 'A', 'B'))

    def test_shared_writer_prevents_b_running_but_not_backend_supersession(self):
        w = World(); w.supersede('B')
        self.assertFalse(w.push('B', 'H0', 'B-result'))
        self.assertTrue(w.push('A', 'H0', 'A-result'))
        # B did not update main, so exact-head protection cannot reject A.
        self.assertEqual(w.remote_head, 'A-result')

    def test_fail_closed_outage_prevents_newly_authorized_push(self):
        w = World(); w.available = False
        permitted = w.fence('A')
        self.assertFalse(permitted)
        self.assertEqual(w.remote_head, 'H0')
        # Later availability permits a fresh attempt, but not a stale token.
        w.available = True; w.supersede('B')
        self.assertTrue(w.fence('B'))
        self.assertFalse(w.fence('A'))

    def test_old_terminal_result_exact_match_cannot_close_b(self):
        w = World(); w.supersede('B')
        if w.current == 'A':
            w.current = None
        self.assertEqual(w.current, 'B')

    def test_repeated_supersession_can_be_bounded_without_liveness(self):
        # Illustrative policy only: no threshold or budget recommendation.
        minimum_age, budget = 600, 3
        last, dispatches = 0, 1
        for now in range(1, 3601):
            if now - last >= minimum_age and dispatches < budget:
                dispatches += 1; last = now
        self.assertEqual(dispatches, 3)
        # This controls cost, not the stale-push counterexamples above.

    def test_expiring_publish_lock_reintroduces_the_stale_push_race(self):
        w = World(); locked_until = 600
        self.assertTrue(w.fence('A'))
        now = locked_until + 1  # A paused or push result still uncertain
        self.assertGreater(now, locked_until)
        w.supersede('B')
        self.assertTrue(w.push('A', 'H0', 'late-A'))

    def test_nonexpiring_publish_lock_blocks_recovery_after_worker_loss(self):
        lock_owner, worker_alive = 'A', False
        recovery_allowed = lock_owner is None
        self.assertFalse(worker_alive)
        self.assertFalse(recovery_allowed)


if __name__ == '__main__':
    unittest.main()
