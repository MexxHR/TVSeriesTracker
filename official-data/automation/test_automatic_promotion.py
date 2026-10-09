"""Fixture coverage for Android-request staging and independent promotion."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import automatic_promotion as automatic
import manual_promotion_guard as production_guard
import monitored
import promotion
from test_promotion import PromotionTests


ID = 999001
TITLE = "Harbor Signal"
URL = "https://www.netflix.com/tudum/articles/harbor-signal-renewed-season-2"
NOW = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)


class AutomaticPromotionTests(unittest.TestCase):
    def setUp(self):
        fixture = PromotionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        sources = (fixture.data_path, fixture.audit_path, fixture.registry_path,
                   fixture.monitored_path, fixture.monitored_audit_path)
        for name, source in zip(production_guard.PROTECTED, sources):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read_bytes())
        (self.root / production_guard.PROTECTED[3]).write_text(
            '{"schemaVersion":1,"series":[]}\n', encoding="utf-8")
        (self.root / production_guard.PROTECTED[4]).write_bytes(b"")
        (self.root / "README.txt").write_text("fixture", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.commit("baseline")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True)

    def commit(self, message):
        self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-q", "-m", message)

    def process(self, *, tmdb_id=ID, now=NOW, unknown=False):
        meta = {"tmdbId": tmdb_id, "title": TITLE,
                "networks": [{"name": "Netflix"}],
                "homepage": "https://www.netflix.com/title/80999001"}
        report = {"provider": None if unknown else "NETFLIX",
                  "result": "insufficient_evidence" if unknown else "candidate_found",
                  "reason": "unknown_provider" if unknown else None,
                  "candidateUrls": [] if unknown else [URL],
                  "candidates": [] if unknown else [{"candidateUrl": URL,
                                                      "factualParserEligible": True,
                                                      "evidenceLevel": "STRONG"}]}
        return monitored.process(
            tmdb_id, dry_run=False, metadata=meta, discoverer=lambda *_: report,
            fetcher=lambda *_: ('<h1>Harbor Signal Season 2</h1>'
                                '<p>Harbor Signal has been renewed for Season 2.</p>', URL),
            now=now, today=now.date(), registry_path=self.root / production_guard.PROTECTED[3],
            audit_path=self.root / production_guard.PROTECTED[4])

    def promote(self, *, tmdb_id=ID, now=NOW):
        return promotion.promote(
            tmdb_id, dry_run=False, now=now,
            production_data=self.root / production_guard.PROTECTED[0],
            production_audit=self.root / production_guard.PROTECTED[1],
            production_registry=self.root / production_guard.PROTECTED[2],
            monitored_registry=self.root / production_guard.PROTECTED[3])

    def test_new_verified_series_promoted_once_then_no_change(self):
        stage_snapshot = Path(self.temp.name) / "stage-before"
        automatic.snapshot_stage(self.root, stage_snapshot, ID)
        staged = self.process()
        self.assertEqual(staged["pipelineState"], "VERIFIED_FACTS")
        self.assertEqual(automatic.verify_stage(self.root, stage_snapshot, ID, staged),
                         ("VERIFIED_FACTS", True))
        self.assertEqual(production_guard.changed_paths(self.root), automatic.MONITORED_WRITABLE)
        self.commit("monitored stage")
        production_snapshot = Path(self.temp.name) / "production-before"
        production_guard.preflight(self.root, production_snapshot, ID)
        first = self.promote()
        self.assertEqual(first["promotionState"], "PROMOTED")
        self.assertTrue(production_guard.verify(self.root, production_snapshot, ID, first,
                                                first_one_piece_acceptance=False))
        canonical = json.loads((self.root / production_guard.PROTECTED[0]).read_text(encoding="utf-8"))
        record = next(row for row in canonical["series"] if row["tmdbId"] == ID)
        self.assertEqual((record["status"], record["nextSeasonNumber"]), ("RENEWED", 2))
        self.commit("production promotion")

        second_stage = Path(self.temp.name) / "second-stage"
        automatic.snapshot_stage(self.root, second_stage, ID)
        staged_again = self.process(now=NOW + dt.timedelta(days=1))
        self.assertEqual(automatic.verify_stage(self.root, second_stage, ID, staged_again),
                         ("VERIFIED_FACTS", False))
        second_production = Path(self.temp.name) / "second-production"
        production_guard.preflight(self.root, second_production, ID)
        second = self.promote(now=NOW + dt.timedelta(days=1))
        self.assertEqual(second["promotionState"], "NO_CHANGE")
        self.assertFalse(production_guard.verify(self.root, second_production, ID, second,
                                                 first_one_piece_acceptance=False))
        self.assertEqual(production_guard.changed_paths(self.root), set())

    def test_disabled_and_malformed_gate_never_enters_production(self):
        for raw in ("", "false", "0", "TRUEE", "yes", "1", "enabled", "   ", " true ", "TRUE"):
            with self.subTest(raw=raw):
                self.assertFalse(automatic.gate_enabled(raw))
        self.assertTrue(automatic.gate_enabled("true"))
        before = tuple((self.root / name).read_bytes() for name in production_guard.PROTECTED[:2])
        stage_snapshot = Path(self.temp.name) / "disabled-stage"
        automatic.snapshot_stage(self.root, stage_snapshot, ID)
        staged = self.process()
        self.assertEqual(automatic.verify_stage(self.root, stage_snapshot, ID, staged),
                         ("VERIFIED_FACTS", True))
        self.assertEqual(before, tuple((self.root / name).read_bytes()
                                       for name in production_guard.PROTECTED[:2]))

    def test_unsupported_provider_and_trusted_source_never_promote(self):
        before = tuple((self.root / name).read_bytes() for name in production_guard.PROTECTED[:2])
        snapshot = Path(self.temp.name) / "unsupported-stage"
        automatic.snapshot_stage(self.root, snapshot, 62425)
        report = self.process(tmdb_id=62425, unknown=True)
        self.assertEqual(report["pipelineState"], "UNSUPPORTED_PROVIDER")
        self.assertEqual(automatic.verify_stage(self.root, snapshot, 62425, report),
                         ("UNSUPPORTED_PROVIDER", True))
        self.assertEqual(before, tuple((self.root / name).read_bytes()
                                       for name in production_guard.PROTECTED[:2]))
        self.commit("unsupported staging")
        trusted_id = json.loads((self.root / production_guard.PROTECTED[2]).read_text(
            encoding="utf-8"))["series"][0]["tmdbId"]
        trusted_snapshot = Path(self.temp.name) / "trusted-stage"
        automatic.snapshot_stage(self.root, trusted_snapshot, trusted_id)
        trusted_report = self.process(tmdb_id=trusted_id)
        self.assertEqual(trusted_report["pipelineState"], "ALREADY_TRUSTED")
        self.assertEqual(automatic.verify_stage(self.root, trusted_snapshot, trusted_id, trusted_report),
                         ("ALREADY_TRUSTED", False))

    def test_unexpected_stage_files_fail_closed(self):
        snapshot = Path(self.temp.name) / "unexpected-stage"
        automatic.snapshot_stage(self.root, snapshot, ID)
        report = self.process()
        (self.root / "README.txt").write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unexpected monitored-stage files"):
            automatic.verify_stage(self.root, snapshot, ID, report)
        (self.root / "README.txt").write_text("fixture", encoding="utf-8")
        (self.root / "unexpected.txt").write_text("untracked", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unexpected monitored-stage files"):
            automatic.verify_stage(self.root, snapshot, ID, report)

    def test_monitored_change_to_another_id_fails_closed(self):
        snapshot = Path(self.temp.name) / "other-id-stage"
        automatic.snapshot_stage(self.root, snapshot, ID)
        report = self.process()
        path = self.root / production_guard.PROTECTED[3]
        registry = json.loads(path.read_text(encoding="utf-8"))
        extra = json.loads(json.dumps(registry["series"][0]))
        extra["tmdbId"] = ID + 1
        registry["series"].append(extra)
        path.write_text(json.dumps(registry) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "another tmdbId"):
            automatic.verify_stage(self.root, snapshot, ID, report)

    def test_report_evidence_must_equal_persisted_verified_row(self):
        snapshot = Path(self.temp.name) / "tampered-report"
        automatic.snapshot_stage(self.root, snapshot, ID)
        report = self.process()
        report["verifiedFacts"] = {"status": "RENEWED", "nextSeasonNumber": 99}
        with self.assertRaisesRegex(ValueError, "evidence differs"):
            automatic.verify_stage(self.root, snapshot, ID, report)


if __name__ == "__main__":
    unittest.main()
