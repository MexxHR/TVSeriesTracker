"""Regression checks for the Android CI release-verification gate."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import package_release as release
from check_apk_signing import certificate_digest


class PackageReleaseTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "project"
        self.out = base / "artifacts"
        self.root.mkdir()
        self.out.mkdir()
        for name in release.TOP:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test source\n", encoding="utf-8")
        for name in release.DIRS:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        remote_source = self.root / "app/src/main/java/com/example/tvseriestracker/data/remote/TmdbRemoteDataSource.kt"
        remote_source.parent.mkdir(parents=True, exist_ok=True)
        remote_source.write_text("// test source\n", encoding="utf-8")
        self.source_apk = self.root / "app/build/outputs/apk/debug/app-debug.apk"
        self.source_apk.parent.mkdir(parents=True, exist_ok=True)
        self.write_apk(b"clean test bytes")
        self.apk = self.out / "test.apk"
        self.zip = self.out / "test.zip"
        for name, value in (("ROOT", self.root), ("APK", self.apk), ("ZIP", self.zip)):
            replacement = patch.object(release, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def write_apk(self, content):
        with ZipFile(self.source_apk, "w") as archive:
            archive.writestr("classes.dex", content)

    def test_optional_gitignore_absent_still_scans_clean_apk_and_zip(self):
        (self.root / "local.properties").write_text("LOCAL_TEST_KEY=not-exported\n", encoding="utf-8")
        (self.root / "official-data/official_series_data.json").write_text("{}", encoding="utf-8")
        (self.root / "app/src/debug.keystore").write_bytes(b"test keystore")
        (self.root / "backend/.env.production").write_bytes(b"test environment settings")
        (self.root / "backend/build/cache.bin").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "backend/build/cache.bin").write_bytes(b"test build cache")
        release.package()
        self.assertTrue(self.apk.is_file())
        with ZipFile(self.zip) as archive:
            names = archive.namelist()
            self.assertNotIn(".gitignore", names)
            self.assertEqual(archive.read("app/build.gradle.kts"),
                             (self.root / "app/build.gradle.kts").read_bytes())
            self.assertNotIn("official-data/official_series_data.json", names)
            self.assertNotIn("local.properties", names)
            self.assertNotIn("app/src/debug.keystore", names)
            self.assertNotIn("backend/.env.production", names)
            self.assertNotIn("backend/build/cache.bin", names)

    def test_optional_gitignore_is_included_when_present(self):
        (self.root / ".gitignore").write_text("build/\n", encoding="utf-8")
        release.package()
        with ZipFile(self.zip) as archive:
            self.assertIn(".gitignore", archive.namelist())

    def test_missing_required_manifest_file_fails_before_artifacts(self):
        (self.root / "settings.gradle.kts").unlink()
        with self.assertRaises(FileNotFoundError):
            release.package()
        self.assertFalse(self.apk.exists())
        self.assertFalse(self.zip.exists())

    def test_missing_apk_fails(self):
        self.source_apk.unlink()
        with self.assertRaises(FileNotFoundError):
            release.package()

    def test_apk_credential_pattern_still_fails(self):
        self.write_apk(b"ghp_" + b"x" * 30)
        with self.assertRaisesRegex(ValueError, "Credential-like value in APK entry"):
            release.package()

    def test_source_zip_credential_pattern_still_fails(self):
        (self.root / "backend/extra.py").write_bytes(b"github_pat_" + b"x" * 30)
        with self.assertRaisesRegex(ValueError, "Credential-like value in project ZIP entry"):
            release.package()

    def test_local_secret_value_in_apk_still_fails(self):
        secret = b"test-only-hmac-secret-value"
        (self.root / "local.properties").write_bytes(b"CLIENT_IP_HMAC_KEY=" + secret + b"\n")
        self.write_apk(secret)
        with self.assertRaisesRegex(ValueError, "Credential-like value in APK entry"):
            release.package()

    def test_unsigned_apk_fails_v2_signer_check(self):
        with self.assertRaises(ValueError):
            certificate_digest(self.source_apk)


if __name__ == "__main__":
    unittest.main()
