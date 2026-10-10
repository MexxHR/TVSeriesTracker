"""Keep Disney+ coverage investigation separate from production writes."""
from pathlib import Path
import unittest


class DisneyCoverageWorkflowTest(unittest.TestCase):
    def test_manual_read_only_workflow_has_integrity_gates(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/disney-plus-discovery-diagnostics.yml').read_text(encoding='utf-8')
        helper = (root / 'official-data/automation/disney_coverage_diagnostics.py').read_text(encoding='utf-8')
        self.assertIn('name: Disney+ Discovery Coverage Diagnostics', workflow)
        self.assertIn('workflow_dispatch:', workflow)
        self.assertIn('permissions:\n  contents: read', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('TMDB_API_TOKEN: ${{ secrets.TMDB_API_TOKEN }}', workflow)
        self.assertIn('python official-data/automation/disney_coverage_diagnostics.py', workflow)
        self.assertIn('sha256sum -c', workflow)
        self.assertIn('git diff --exit-code HEAD', workflow)
        self.assertIn('disney-plus-discovery-coverage-diagnostics.json', workflow)
        for identifier in ('103540', '138503', '138502'):
            self.assertIn(identifier, helper)
        for forbidden in ('  push:', '  schedule:', 'contents: write', 'git push',
                          'git add', '--promote-monitored', 'continue-on-error'):
            self.assertNotIn(forbidden, workflow)

    def test_version_remains_v2721(self):
        root = Path(__file__).resolve().parents[1]
        gradle = (root / 'app/build.gradle.kts').read_text(encoding='utf-8')
        room = (root / 'app/src/main/java/com/example/tvseriestracker/data/TrackingDatabase.kt').read_text(encoding='utf-8')
        self.assertIn('versionCode = 27', gradle)
        self.assertIn('versionName = "2.7.2.1"', gradle)
        self.assertIn('version = 3', room)


if __name__ == '__main__':
    unittest.main()
