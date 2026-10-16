"""Contract feasibility counterexamples, not an implemented lease runtime.

No network, real clock waiting, canonical reads or production writes.
The hypothetical TTLs below are illustrative, never configuration defaults.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
WRITERS = (
    'process-series-request.yml', 'update-official-data.yml',
    'process-discovered-series.yml', 'promote-monitored-series.yml',
    'preview-monitored-promotion.yml',
)


def backend_observation(now, attempt='opaque-A', started=0):
    # All permitted observations before a terminal callback, in either world.
    return (attempt, started, 'ACTIVE', now, None)


class LeaseExpirySafetyGateTests(unittest.TestCase):
    def test_current_workflow_has_no_explicit_runtime_or_queue_deadline(self):
        workflow = (ROOT / '.github/workflows/process-series-request.yml').read_text()
        self.assertIn('  monitored_update:', workflow)
        self.assertIn('  promote:', workflow)
        self.assertNotIn('timeout-minutes:', workflow)
        self.assertIn('  queue: max', workflow)
        self.assertIn('  cancel-in-progress: false', workflow)

    def test_multiple_existing_writers_share_the_dispatch_queue(self):
        for name in WRITERS:
            with self.subTest(workflow=name):
                source = (ROOT / '.github/workflows' / name).read_text()
                self.assertIn('  group: official-data-writes', source)
                self.assertIn('  queue: max', source)
                self.assertNotIn('timeout-minutes:', source)

    def test_lost_and_healthy_attempts_are_indistinguishable_at_expiry(self):
        for ttl in (240, 600, 1800, 3600):
            with self.subTest(illustrative_ttl=ttl):
                lost = backend_observation(ttl + 1)
                healthy_queued = backend_observation(ttl + 1)
                healthy_running = backend_observation(ttl + 1)
                self.assertEqual(lost, healthy_queued)
                self.assertEqual(lost, healthy_running)
                # Any deterministic expiry decision given these observations
                # is identical. The correct safe decisions are different.
                should_recover_lost = True
                should_duplicate_healthy = False
                self.assertNotEqual(should_recover_lost, should_duplicate_healthy)

    def test_healthy_active_at_expiry_retry_queues_a_second_attempt(self):
        # Counterexample for a proposed expiry-only lease, NOT current service.
        ttl = 600
        runs = [{'attempt': 'opaque-A', 'state': 'running', 'end': ttl + 120}]
        now = ttl + 1
        expiry_allows_recovery = now > ttl
        self.assertTrue(expiry_allows_recovery)
        self.assertLess(now, runs[0]['end'])
        runs.append({'attempt': 'opaque-B', 'state': 'queued'})
        # Shared serialization limits simultaneous writers, not dispatch count.
        self.assertEqual(sum(r['state'] == 'running' for r in runs), 1)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]['state'], 'running')

    def test_queued_attempt_can_expire_before_its_first_job_starts(self):
        ttl = 600
        # Existing other writer holds the workflow-level group. The local YAML
        # supplies no smaller bound excluding this synthetic permitted schedule.
        writer_releases_at = ttl + 120
        now = ttl + 1
        self.assertLess(now, writer_releases_at)
        accepted_runs = ['opaque-A']
        accepted_runs.append('opaque-B')  # expiry-only recovery decision
        self.assertEqual(len(accepted_runs), 2)
        # A heartbeat inside A could not have run while A was queued.
        self.assertGreater(writer_releases_at, ttl)


if __name__ == '__main__':
    unittest.main()
