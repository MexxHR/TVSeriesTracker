"""Protect the read-only GitHub acceptance path for the AMC parser hotfix."""
from pathlib import Path
import unittest


class AudacityPreviewWorkflowTest(unittest.TestCase):
    def test_hotfix_version_and_existing_android_ci_gates(self):
        root = Path(__file__).resolve().parents[1]
        gradle = (root / 'app/build.gradle.kts').read_text(encoding='utf-8')
        android = (root / '.github/workflows/android-debug.yml').read_text(encoding='utf-8')
        package = (root / 'backend/package_release.py').read_text(encoding='utf-8')
        self.assertIn('versionCode = 27', gradle)
        self.assertIn('versionName = "2.7.2.1"', gradle)
        self.assertIn('VERSION = "V2.7.2.1"', package)
        for required in ('android-actions/setup-android@v3', "packages: 'platform-tools'",
                         'chmod +x gradlew', './gradlew clean :app:testDebugUnitTest :app:assembleDebug',
                         'python backend/package_release.py', 'certificate_digest(',
                         'TV-Series-Tracker-V2.7.2.1-debug'):
            self.assertIn(required, android)

    def test_manual_preview_runs_real_monitored_path_and_independent_rebuild(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/validate-audacity-production-parser.yml').read_text(encoding='utf-8')
        self.assertIn('workflow_dispatch:', workflow)
        self.assertIn('permissions:\n  contents: read', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('TMDB_API_TOKEN: ${{ secrets.TMDB_API_TOKEN }}', workflow)
        self.assertIn('monitored.process(tmdb_id, dry_run=True)', workflow)
        self.assertIn('promotion._validate_evidence(row, domains)', workflow)
        self.assertIn('promotion._facts_summary(facts, row,', workflow)
        self.assertIn('sha256sum -c', workflow)
        self.assertIn('git diff --exit-code HEAD', workflow)
        self.assertIn('audacity-production-parser-preview.json', workflow)
        for forbidden in ('  push:', '  schedule:', 'contents: write', 'git push',
                          'git add', '--promote-monitored', 'continue-on-error'):
            self.assertNotIn(forbidden, workflow)


if __name__ == '__main__':
    unittest.main()
