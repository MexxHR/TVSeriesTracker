"""V2.7.16.1 regressions for packaged, deterministic fixture support.

The actual runner failure was module import, not Paramount network output.
All HTTP boundaries below are controlled mocks; no external service is used.
"""
import ast
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import provider_coverage_audit as audit
import v2716_test_support as support


class TestSupportPackagingTests(unittest.TestCase):
    def test_missing_historical_module_reproduces_the_import_failure(self):
        import importlib.abc
        class AbsentHistoricalModule(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname in ('test_v273_coverage_recovery', 'test_v2715_coverage_recovery'):
                    raise ModuleNotFoundError('No module named ' + repr(fullname), name=fullname)
        blocker = AbsentHistoricalModule()
        sys.meta_path.insert(0, blocker)
        try:
            for name in ('test_v273_coverage_recovery', 'test_v2715_coverage_recovery'):
                # exec is a faithful import-time reproduction without calling HTTP.
                with patch.dict(sys.modules):
                    sys.modules.pop(name, None)
                    with self.assertRaisesRegex(ModuleNotFoundError, name):
                        exec('import ' + name, {})
        finally:
            sys.meta_path.remove(blocker)

    def test_cold_workflow_discovery_without_old_test_modules_or_report(self):
        code = r'''
import importlib.abc, pathlib, sys, unittest
class AbsentHistoricalModule(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('test_v273_coverage_recovery', 'test_v2715_coverage_recovery'):
            raise ModuleNotFoundError('No module named ' + repr(fullname), name=fullname)
sys.meta_path.insert(0, AbsentHistoricalModule())
original = pathlib.Path.is_file
def checkout_file(path):
    return False if path.name == 'V2.7.15_FORENSICS.md' else original(path)
pathlib.Path.is_file = checkout_file
suite = unittest.defaultTestLoader.discover('official-data/automation', pattern='test_v2716_audit_fidelity.py')
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(not result.wasSuccessful())
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=audit.ROOT,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Ran 10 tests', result.stderr)
        self.assertNotIn('_FailedTest', result.stderr)

    def test_integration_and_support_import_no_sibling_test_module(self):
        for name in ('test_v2716_audit_fidelity.py', 'v2716_test_support.py'):
            tree = ast.parse((Path(__file__).parent/name).read_text(encoding='utf-8'))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or '')
            self.assertFalse([name for name in imports if name.startswith('test_')], imports)

    def test_four_classifications_do_not_contact_a_fake_429_live_boundary(self):
        def fake_live(url, *args, **kwargs):
            raise HTTPError(url, 429, 'controlled test only', {}, None)
        with patch.object(audit.update, 'fetch', side_effect=fake_live) as live, \
             patch.object(audit.update, 'build_opener', side_effect=AssertionError('UNEXPECTED_LIVE_NETWORK_ACCESS')) as opener:
            for option in support.CAPTURE['forensicCases']:
                with self.subTest(case=option['caseId']):
                    row, _ = support.replay_case(option['caseId'])
                    self.assertEqual(row['auditFidelityFinding'], option['expectedFinding'])
            live.assert_not_called()
            opener.assert_not_called()

    def test_github_actions_environment_alone_is_not_runner_confirmation(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS':'true'}), \
             patch.object(audit, '_wbd_access_probe', return_value={'status':'TEST_FIXTURE'}), \
             patch.object(audit, '_fx_control_audit', return_value={'status':'TEST_FIXTURE'}):
            report = audit.build_report(audit.load_corpus(), token=None)
        self.assertEqual(report['resultScope'], 'LOCAL_DIAGNOSTIC_ONLY')
        self.assertEqual(report['finalCoverageStatus'], 'AWAITING_GITHUB_RUNNER_CONFIRMATION')
        self.assertFalse(report['runnerAuthorityConfirmed'])

    def test_actual_audit_default_fetch_boundary_is_unchanged(self):
        url = 'https://www.paramountpressexpress.com/show/releases/'
        fetcher = audit.BoundedFetcher('PARAMOUNT')
        self.assertIsNone(fetcher.transport)
        with patch.object(audit.update, 'fetch', return_value=('<h1>Controlled boundary</h1>', url)) as live:
            self.assertEqual(fetcher(url, ['paramountpressexpress.com'])[1], url)
        live.assert_called_once_with(url, ['paramountpressexpress.com'])

    def test_explicit_runner_cli_still_passes_live_audit_mode(self):
        with patch.object(audit, 'load_corpus', return_value={}), \
             patch.object(audit.discovery, 'local_tmdb_token', return_value=None), \
             patch.object(audit, 'build_report', return_value={'auditQuality':{'valid':True}}) as build, \
             patch.object(audit, 'write_report'), patch.object(audit, 'render_markdown', return_value=''), \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(audit.main(['--output-dir','unused-test-output','--execution-mode','runner-confirmation']), 0)
        self.assertEqual(build.call_args.kwargs['execution_mode'], 'runner-confirmation')
        self.assertNotIn('transport', build.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
