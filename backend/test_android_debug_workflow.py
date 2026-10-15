"""Manual Android build must reject stale or absent outputs before upload."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from check_debug_metadata import check


ROOT = Path(__file__).resolve().parents[1]


class AndroidDebugWorkflowTest(unittest.TestCase):
    def test_manual_read_only_build_has_required_gates(self):
        source = (ROOT / '.github/workflows/android-debug.yml').read_text(encoding='utf-8')
        self.assertIn('name: Android debug APK\non:\n  workflow_dispatch:', source)
        for fragment in ('java-version: \'17\'', 'chmod +x gradlew',
                         './gradlew clean :app:testDebugUnitTest :app:assembleDebug',
                         'test -s app/build/outputs/apk/debug/app-debug.apk',
                         'python backend/check_debug_metadata.py', 'python backend/package_release.py',
                         'certificate_digest(', 'if-no-files-found: error',
                         'TV-Series-Tracker-V2.7.16.1-debug.apk'):
            self.assertIn(fragment, source)
        for fragment in ('  push:', '  schedule:', '  pull_request:', 'contents: write',
                         'TMDB_API_TOKEN', 'git push', '--promote-monitored', 'continue-on-error'):
            self.assertNotIn(fragment, source)
        self.assertNotIn('TMDB_API_TOKEN', (ROOT / 'app/build.gradle.kts').read_text())

    def _fixture(self, root):
        gradle = root / 'build.gradle.kts'
        gradle.write_text('applicationId = "com.example.tvseriestracker"\nversionCode = 48\nversionName = "2.7.16.1"')
        metadata = {'applicationId': 'com.example.tvseriestracker', 'variantName': 'debug',
                    'elements': [{'versionCode': 48, 'versionName': '2.7.16.1', 'outputFile': 'app-debug.apk'}]}
        path = root / 'output-metadata.json'
        (root / 'app-debug.apk').write_bytes(b'isolated test fixture, not an installable APK')
        return gradle, path, metadata

    def test_current_debug_metadata_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            gradle, path, metadata = self._fixture(Path(directory))
            path.write_text(json.dumps(metadata))
            self.assertEqual(check(path, gradle)['versionCode'], 48)

    def test_stale_wrong_application_or_variant_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            gradle, path, metadata = self._fixture(Path(directory))
            for key, value in [('applicationId', 'another.app'), ('variantName', 'release')]:
                wrong = copy.deepcopy(metadata); wrong[key] = value
                path.write_text(json.dumps(wrong))
                with self.subTest(key=key), self.assertRaises(ValueError): check(path, gradle)
            for key, value in [('versionCode', 40), ('versionName', '2.7.11'), ('outputFile', '../old.apk')]:
                wrong = copy.deepcopy(metadata); wrong['elements'][0][key] = value
                path.write_text(json.dumps(wrong))
                with self.subTest(key=key), self.assertRaises(ValueError): check(path, gradle)

    def test_empty_or_missing_apk_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); gradle, path, metadata = self._fixture(root)
            path.write_text(json.dumps(metadata))
            (root / 'app-debug.apk').write_bytes(b'')
            with self.assertRaises(ValueError): check(path, gradle)
            (root / 'app-debug.apk').unlink()
            with self.assertRaises(ValueError): check(path, gradle)


if __name__ == '__main__':
    unittest.main()
