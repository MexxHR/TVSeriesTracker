"""Controlled manual promotion uses the real publisher on isolated Git fixtures."""
import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import manual_promotion_guard as guard
import promotion
import update
from test_promotion import PromotionTests


class ManualPromotionGuardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = PromotionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.snapshot = Path(self.temp.name) / "snapshot"
        originals = (self.fixture.data_path, self.fixture.audit_path,
                     self.fixture.registry_path, self.fixture.monitored_path,
                     self.fixture.monitored_audit_path)
        for name, original in zip(guard.PROTECTED, originals):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original.read_bytes())
        self.git("init", "-q", "-b", "main")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-q", "-m", "baseline")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def promote(self, tmdb_id=111110, now=None):
        return promotion.promote(
            tmdb_id, now=now or self.fixture.now,
            production_data=self.root / guard.PROTECTED[0],
            production_audit=self.root / guard.PROTECTED[1],
            production_registry=self.root / guard.PROTECTED[2],
            monitored_registry=self.root / guard.PROTECTED[3],
        )

    def test_first_promotion_and_second_no_change(self):
        guard.preflight(self.root, self.snapshot, 111110)
        first = self.promote()
        self.assertEqual(first["promotionState"], "PROMOTED")
        self.assertTrue(guard.verify(self.root, self.snapshot, 111110, first))
        self.assertEqual(guard.changed_paths(self.root), guard.WRITABLE)
        self.git("add", "--", *sorted(guard.WRITABLE))
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-q", "-m", "promotion")
        second_snapshot = Path(self.temp.name) / "second"
        guard.preflight(self.root, second_snapshot, 111110)
        second = self.promote(now=self.fixture.now + dt.timedelta(days=1))
        self.assertEqual(second["promotionState"], "NO_CHANGE")
        self.assertFalse(guard.verify(self.root, second_snapshot, 111110, second))
        self.assertEqual(guard.changed_paths(self.root), set())

    def test_dark_matter_rejected_without_write(self):
        monitored_path = self.root / guard.PROTECTED[3]
        registry = json.loads(monitored_path.read_text(encoding="utf-8"))
        registry["series"].insert(0, {"tmdbId": 62425, "title": "Dark Matter", "provider": None,
                                      "discoveryState": "UNSUPPORTED_PROVIDER", "candidateUrls": [],
                                      "verifiedFacts": {}, "sourceEvidence": [], "lastChecked": "2026-10-01"})
        monitored_path.write_text(json.dumps(registry) + "\n", encoding="utf-8")
        self.git("add", "--", guard.PROTECTED[3])
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-q", "-m", "unsupported monitored record")
        guard.preflight(self.root, self.snapshot, 62425)
        report = self.promote(62425)
        self.assertEqual(report["promotionState"], "NOT_VERIFIED")
        with self.assertRaisesRegex(ValueError, "not approved"):
            guard.verify(self.root, self.snapshot, 62425, report)
        self.assertEqual(guard.changed_paths(self.root), set())

    def test_unrelated_and_untracked_mutation_fail_closed(self):
        guard.preflight(self.root, self.snapshot, 111110)
        report = self.promote()
        (self.root / "unexpected.txt").write_text("untracked", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unexpected repository changes"):
            guard.verify(self.root, self.snapshot, 111110, report)
        (self.root / "unexpected.txt").unlink()
        sources = self.root / guard.PROTECTED[2]
        sources.write_bytes(sources.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "Unexpected repository changes"):
            guard.verify(self.root, self.snapshot, 111110, report)

    def test_unrelated_canonical_series_change_fails_closed(self):
        guard.preflight(self.root, self.snapshot, 111110)
        report = self.promote()
        canonical_path = self.root / guard.PROTECTED[0]
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        unrelated = next(row for row in canonical["series"] if row["tmdbId"] != 111110)
        unrelated["title"] += " altered"
        canonical_path.write_text(json.dumps(canonical, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises((ValueError, update.AutomationError)):
            guard.verify(self.root, self.snapshot, 111110, report)

    def test_audit_mismatch_fails_closed(self):
        guard.preflight(self.root, self.snapshot, 111110)
        report = self.promote()
        audit_path = self.root / guard.PROTECTED[1]
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[-1])
        entry["tmdbId"] = 62425
        lines[-1] = json.dumps(entry, separators=(",", ":"))
        audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "audit"):
            guard.verify(self.root, self.snapshot, 111110, report)

    def test_only_positive_decimal_id_accepted(self):
        for value in ("0", "-1", "1;echo hi", "1.0", "", "01", "9" * 19):
            with self.subTest(value=value), self.assertRaises(ValueError):
                guard.positive_id(value)
        self.assertEqual(guard.positive_id("111110"), 111110)

    def test_automatic_verified_stage_rejects_unexpected_promotion_state(self):
        guard.preflight(self.root, self.snapshot, 111110)
        for state in ("NOT_VERIFIED", "TRUSTED_SOURCE_PRECEDENCE", "NOT_FOUND",
                      "VALIDATION_FAILED", "CONFLICT"):
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "not approved"):
                guard.verify(self.root, self.snapshot, 111110,
                             {"tmdbId": 111110, "dryRun": False, "fatal": False,
                              "promotionState": state, "wouldPublish": False},
                             first_one_piece_acceptance=False)


if __name__ == "__main__":
    unittest.main()
