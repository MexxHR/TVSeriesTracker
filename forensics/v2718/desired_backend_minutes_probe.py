"""HISTORICAL_LEGACY_IMMEDIATE_DISPATCH_PROBE; intentionally red in legacy mode.

Separate from test_*.py suites. This probe is evidence for the explicit gate,
not a controlled-batch production requirement. Dispatch-count recovery is obsolete
for the new queue architecture. Assertions stay unchanged; local fake work/API only.
"""
import unittest
from test_backend_recovery_gate import BackendGateTests

class MinutesRecoveryProbe(BackendGateTests):
    # Select only this method from __main__; inherited counterexample tests are
    # not rerun as part of the deliberately red probe.
    def test_accepted_then_failed_work_can_redispatch_within_minutes(self):
        self.failed_acknowledged_request()
        self.now+=240
        self.post()
        self.assertEqual(len(self.workflow.dispatches),2,
            'BACKEND_RECOVERY_BLOCKER: 202 already_queued blocks new work despite no active workflow or canonical fact')

if __name__=='__main__':
    suite=unittest.TestSuite([MinutesRecoveryProbe('test_accepted_then_failed_work_can_redispatch_within_minutes')])
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
