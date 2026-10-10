import copy
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import discovery
import monitored
import promotion
import update


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data_path = root / "official_series_data.json"
        self.audit_path = root / "changes.jsonl"
        self.monitored_path = root / "monitored_series.json"
        self.monitored_audit_path = root / "monitored_changes.jsonl"
        self.registry_path = root / "sources.json"
        self.registry = json.loads(update.REGISTRY.read_text(encoding="utf-8"))
        self.production = json.loads((Path(__file__).parent / "fixtures" / "v235_canonical.json").read_text(encoding="utf-8"))
        self.data_path.write_text(json.dumps(self.production, indent=2) + "\n", encoding="utf-8")
        self.registry_path.write_text(json.dumps(self.registry), encoding="utf-8")
        self.audit_path.write_bytes(update.AUDIT.read_bytes())
        self.monitored_audit_path.write_bytes(b'{"existing":"monitored"}\n')
        self.now = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
        self.row = self.one_piece_row()
        self.write_monitored([self.row])

    def write_monitored(self, rows, *, validate=True):
        data = {"schemaVersion": 1, "series": sorted(rows, key=lambda row: row["tmdbId"])}
        if validate:
            monitored.validate_registry(data)
        self.monitored_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def one_piece_row(self):
        tmdb_id = 111110
        title = "ONE PIECE"
        provider = "NETFLIX"
        domains = discovery.provider_specs()[provider]["officialDomains"]
        entry = {"tmdbId": tmdb_id, "title": title, "aliases": [title], "allowedDomains": domains}
        urls_and_text = [
            ("https://www.netflix.com/tudum/articles/one-piece-renewed-season-3",
             "ONE PIECE has been renewed for Season 3."),
            ("https://www.netflix.com/tudum/articles/one-piece-season-2-release-date",
             "ONE PIECE Season 2 premiered on March 10, 2026."),
            ("https://www.netflix.com/tudum/articles/one-piece-season-3-release-window",
             "ONE PIECE Season 3 premieres in 2027."),
        ]
        facts = []
        for url, excerpt in urls_and_text:
            article = {"title": "", "body": excerpt, "url": url,
                       "sourceName": "Netflix Tudum", "publicationDate": None}
            facts.extend(update.detect(article, entry, self.now.date(),
                                       extended_final=True, strict_binding=True))
        verified, _ = update.merge(None, facts, self.now.date())
        return {"tmdbId": tmdb_id, "title": title, "provider": provider,
                "discoveryState": "VERIFIED_FACTS",
                "candidateUrls": sorted({url for url, _ in urls_and_text}),
                "eligibleSourceUrls": sorted({url for url, _ in urls_and_text}),
                "verifiedFacts": {key: verified.get(key) for key in monitored.FACT_FIELDS},
                "sourceEvidence": [{key: fact.get(key) for key in monitored.EVIDENCE_FIELDS}
                                   for fact in facts],
                "parserResult": "facts", "validationResult": "passed",
                "lastChecked": self.now.date().isoformat()}

    def promote(self, *, dry_run=False, now=None):
        return promotion.promote(111110, dry_run=dry_run, now=now or self.now,
                                 production_data=self.data_path,
                                 production_audit=self.audit_path,
                                 production_registry=self.registry_path,
                                 monitored_registry=self.monitored_path)

    def running_point_row(self):
        return json.loads((Path(__file__).parent / "fixtures" / "running_point_monitored.json")
                          .read_text(encoding="utf-8"))

    def promote_running_point(self, *, dry_run=False):
        return promotion.promote(244623, dry_run=dry_run, now=self.now,
                                 production_data=self.data_path,
                                 production_audit=self.audit_path,
                                 production_registry=self.registry_path,
                                 monitored_registry=self.monitored_path)

    def test_running_point_live_evidence_promotes_once_then_no_change(self):
        row = self.running_point_row()
        self.write_monitored([row])
        first = self.promote_running_point()
        self.assertEqual(first["promotionState"], "PROMOTED", first)
        self.assertEqual(first["proposedProductionRecord"]["status"], "RENEWED")
        self.assertEqual(first["proposedProductionRecord"]["nextSeasonNumber"], 3)
        self.assertEqual(first["proposedProductionRecord"]["sourceUrl"], row["verifiedFacts"]["sourceUrl"])
        after = (self.data_path.read_bytes(), self.audit_path.read_bytes())
        second = self.promote_running_point()
        self.assertEqual(second["promotionState"], "NO_CHANGE", second)
        self.assertFalse(second["wouldPublish"])
        self.assertEqual(second["changedFields"], [])
        self.assertEqual(after, (self.data_path.read_bytes(), self.audit_path.read_bytes()))

    def test_running_point_evidence_order_and_duplicates_do_not_change_source(self):
        row = self.running_point_row()
        expected_source = row["verifiedFacts"]["sourceUrl"]
        original = row["sourceEvidence"]
        for evidence in (original, list(reversed(original)),
                         original[2:] + original[:2], original + [copy.deepcopy(original[0])]):
            with self.subTest(order=[item["sourceUrl"] for item in evidence]):
                variant = copy.deepcopy(row)
                variant["sourceEvidence"] = evidence
                self.write_monitored([variant])
                result = self.promote_running_point(dry_run=True)
                self.assertEqual(result["promotionState"], "PROMOTABLE", result)
                self.assertEqual(result["proposedProductionRecord"]["sourceUrl"], expected_source)

    def test_running_point_mismatch_names_field_and_fails_closed(self):
        row = self.running_point_row()
        row["verifiedFacts"]["sourceUrl"] = row["candidateUrls"][0]
        self.write_monitored([row])
        before = (self.data_path.read_bytes(), self.audit_path.read_bytes())
        result = self.promote_running_point()
        self.assertEqual(result["promotionState"], "VALIDATION_FAILED", result)
        self.assertTrue(result["fatal"])
        self.assertFalse(result["promotionEligible"])
        self.assertFalse(result["wouldPublish"])
        self.assertEqual(result["validationMismatch"]["fields"], ["sourceUrl"])
        self.assertEqual(result["validationMismatch"]["monitored"]["sourceUrl"], row["candidateUrls"][0])
        self.assertEqual(result["validationMismatch"]["rebuilt"]["sourceUrl"],
                         "https://www.netflix.com/tudum/running-point")
        self.assertEqual(before, (self.data_path.read_bytes(), self.audit_path.read_bytes()))

    def test_verified_facts_is_eligible_for_independent_gate(self):
        result = self.promote(dry_run=True)
        self.assertTrue(result["promotionEligible"], result)
        self.assertEqual(result["promotionState"], "PROMOTABLE", result)
        self.assertEqual(result["validationResult"], "passed")

    def test_one_piece_preserves_season_3_and_season_2_date(self):
        result = self.promote(dry_run=True)
        proposed = result["proposedProductionRecord"]
        self.assertEqual(proposed["tmdbId"], 111110)
        self.assertEqual(proposed["title"], "ONE PIECE")
        self.assertEqual(proposed["status"], "RENEWED")
        self.assertEqual(proposed["nextSeasonNumber"], 3)
        self.assertIsNone(proposed["releaseDate"])
        self.assertEqual(proposed["releaseYear"], 2027)
        self.assertEqual(proposed["sourceName"], "Netflix Tudum")
        self.assertEqual(proposed["sourceUrl"], "https://www.netflix.com/tudum/articles/one-piece-renewed-season-3")
        self.assertIsNone(proposed["announcementDate"])
        self.assertNotIn("https://www.netflix.com/tudum/articles/one-piece-season-2-release-date", result["sourceUrls"])
        self.assertFalse(result["fatal"])

    def test_dry_run_writes_nothing(self):
        paths = (self.data_path, self.audit_path, self.registry_path,
                 self.monitored_path, self.monitored_audit_path)
        before = {path: path.read_bytes() for path in paths}
        files_before = set(self.data_path.parent.rglob("*"))
        result = self.promote(dry_run=True)
        self.assertEqual(result["promotionState"], "PROMOTABLE")
        self.assertTrue(result["wouldPublish"])
        self.assertEqual(result["validationResult"], "passed")
        self.assertEqual(result["proposedProductionRecord"]["nextSeasonNumber"], 3)
        self.assertEqual(result["proposedProductionRecord"]["releaseYear"], 2027)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(set(self.data_path.parent.rglob("*")), files_before)

    def test_cli_dry_run_creates_no_bytecode_or_repository_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "official-data"
            automation_dir = data_dir / "automation"
            history_dir = data_dir / "history"
            automation_dir.mkdir(parents=True)
            history_dir.mkdir()
            original = Path(__file__).parent
            for name in ("update.py", "promotion.py", "monitored.py", "discovery.py"):
                shutil.copyfile(original / name, automation_dir / name)
            shutil.copyfile(update.ROOT / "official-data" / "discovery_providers.json",
                            data_dir / "discovery_providers.json")
            copies = {
                data_dir / "official_series_data.json": self.data_path,
                data_dir / "sources.json": self.registry_path,
                data_dir / "monitored_series.json": self.monitored_path,
                history_dir / "changes.jsonl": self.audit_path,
                history_dir / "monitored_changes.jsonl": self.monitored_audit_path,
            }
            for destination, source in copies.items():
                destination.write_bytes(source.read_bytes())
            before = {path: path.read_bytes() for path in copies}
            files_before = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
            env = os.environ.copy()
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            completed = subprocess.run(
                [sys.executable, str(automation_dir / "update.py"),
                 "--promote-monitored", "111110", "--dry-run"],
                cwd=root, env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            report = json.loads(completed.stdout)
            self.assertEqual(report["promotionState"], "PROMOTABLE", report)
            self.assertTrue(report["wouldPublish"], report)
            self.assertEqual({path: path.read_bytes() for path in copies}, before)
            self.assertEqual({path.relative_to(root) for path in root.rglob("*") if path.is_file()}, files_before)

    def test_dark_matter_unsupported_provider_is_safe_noop(self):
        row = {"tmdbId": 62425, "title": "Dark Matter", "provider": None,
               "discoveryState": "UNSUPPORTED_PROVIDER", "candidateUrls": [],
               "verifiedFacts": {}, "sourceEvidence": [], "lastChecked": "2026-10-01"}
        self.write_monitored([self.row, row])
        before_data = self.data_path.read_bytes()
        before_audit = self.audit_path.read_bytes()
        before_generated = json.loads(before_data)["generatedAt"]
        result = promotion.promote(62425, dry_run=False, now=self.now,
                                   production_data=self.data_path,
                                   production_audit=self.audit_path,
                                   production_registry=self.registry_path,
                                   monitored_registry=self.monitored_path)
        self.assertEqual(result["promotionState"], "NOT_VERIFIED")
        self.assertFalse(result["wouldPublish"])
        self.assertEqual(before_data, self.data_path.read_bytes())
        self.assertEqual(before_audit, self.audit_path.read_bytes())
        self.assertEqual(before_generated, json.loads(self.data_path.read_bytes())["generatedAt"])

    def test_every_nonverified_and_unknown_state_is_rejected(self):
        states = ["NO_VERIFIED_FACTS", "NOT_PARSER_ELIGIBLE", "PROVIDER_UNAVAILABLE",
                  "DISCOVERY_INSUFFICIENT", "VALIDATION_FAILED", "UNSUPPORTED_PROVIDER",
                  "METADATA_UNAVAILABLE", "ALREADY_TRUSTED", "FUTURE_STATE"]
        for state in states:
            with self.subTest(state=state):
                row = copy.deepcopy(self.row)
                row["discoveryState"] = state
                if state != "VERIFIED_FACTS":
                    row["verifiedFacts"] = {}
                    row["sourceEvidence"] = []
                self.write_monitored([row], validate=state not in ("ALREADY_TRUSTED", "FUTURE_STATE"))
                result = self.promote(dry_run=True)
                expected = "VALIDATION_FAILED" if state in ("ALREADY_TRUSTED", "FUTURE_STATE") else "NOT_VERIFIED"
                self.assertEqual(result["promotionState"], expected)

    def test_idempotency_second_run_adds_no_audit_or_generated_at_change(self):
        first = self.promote(dry_run=False)
        self.assertEqual(first["promotionState"], "PROMOTED")
        data_after_first = self.data_path.read_bytes()
        audit_after_first = self.audit_path.read_bytes()
        second = self.promote(dry_run=False, now=self.now + dt.timedelta(days=1))
        self.assertEqual(second["promotionState"], "NO_CHANGE")
        self.assertFalse(second["wouldPublish"])
        self.assertEqual(data_after_first, self.data_path.read_bytes())
        self.assertEqual(audit_after_first, self.audit_path.read_bytes())
        row = next(r for r in json.loads(self.data_path.read_bytes())["series"] if r["tmdbId"] == 111110)
        self.assertEqual(row["releaseDate"], None)
        audit_row = json.loads(self.audit_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(audit_row["source"], "monitored automatic promotion")
        self.assertEqual(audit_row["old"], None)
        self.assertEqual(audit_row["new"], row)

    def test_trusted_source_precedence_blocks_monitored_override(self):
        trusted_id = self.registry["series"][0]["tmdbId"]
        row = copy.deepcopy(self.row)
        row["tmdbId"] = trusted_id
        self.write_monitored([row], validate=False)
        result = promotion.promote(trusted_id, dry_run=True, now=self.now,
                                   production_data=self.data_path, production_audit=self.audit_path,
                                   production_registry=self.registry_path, monitored_registry=self.monitored_path)
        self.assertEqual(result["promotionState"], "TRUSTED_SOURCE_PRECEDENCE")

    def test_legacy_verified_record_without_persisted_gate_fails_closed(self):
        row = copy.deepcopy(self.row)
        for key in ("parserResult", "validationResult", "eligibleSourceUrls"):
            row.pop(key)
        self.write_monitored([row])
        result = self.promote(dry_run=True)
        self.assertEqual(result["promotionState"], "VALIDATION_FAILED")
        self.assertTrue(result["fatal"])

    def test_mismatched_season_evidence_rejected(self):
        row = copy.deepcopy(self.row)
        row["sourceEvidence"][0]["nextSeasonNumber"] = 2
        self.write_monitored([row])
        result = self.promote(dry_run=True)
        self.assertEqual(result["promotionState"], "VALIDATION_FAILED")

    def test_conflicting_cancellation_and_renewal_evidence_rejected(self):
        row = copy.deepcopy(self.row)
        url = "https://www.netflix.com/tudum/articles/one-piece-season-3-canceled"
        fact = update.detect({"title": "", "body": "ONE PIECE Season 3 was canceled.",
                              "url": url, "sourceName": "Netflix Tudum", "publicationDate": None},
                             {"tmdbId": 111110, "title": "ONE PIECE", "aliases": ["ONE PIECE"],
                              "allowedDomains": discovery.provider_specs()["NETFLIX"]["officialDomains"]},
                             self.now.date(), extended_final=True, strict_binding=True)[0]
        row["candidateUrls"].append(url)
        row["eligibleSourceUrls"].append(url)
        row["sourceEvidence"].append({**{key: fact.get(key) for key in monitored.EVIDENCE_FIELDS},
                                     "publicationDateSource": "none"})
        self.write_monitored([row])
        result = self.promote(dry_run=True)
        self.assertEqual(result["promotionState"], "CONFLICT")

    def test_nonofficial_or_noneligible_source_rejected(self):
        row = copy.deepcopy(self.row)
        row["sourceEvidence"][0]["sourceUrl"] = "https://netflix.com.evil.example/renewed-season-3"
        self.write_monitored([row], validate=False)
        result = self.promote(dry_run=True)
        self.assertEqual(result["promotionState"], "VALIDATION_FAILED")

    def test_production_wording_and_url_slug_cannot_establish_renewal(self):
        row = copy.deepcopy(self.row)
        row["sourceEvidence"][0]["evidenceText"] = "ONE PIECE Season 3 begins production in 2027."
        self.write_monitored([row])
        result = self.promote(dry_run=True)
        self.assertEqual(result["promotionState"], "VALIDATION_FAILED")

    def test_announcement_date_requires_structured_metadata_provenance(self):
        row = copy.deepcopy(self.row)
        row["sourceEvidence"][0]["announcementDate"] = "2026-08-23"
        self.write_monitored([row])
        result = self.promote(dry_run=True)
        self.assertEqual(result["promotionState"], "VALIDATION_FAILED")
        self.assertIn("structured publication metadata", result["promotionReason"])

    def test_same_season_merge_enrichment_and_preservation(self):
        old = {"tmdbId": 500001, "title": "Example", "nextSeasonNumber": 3,
               "status": "RENEWED", "releaseDate": None, "releaseYear": None,
               "sourceName": "Netflix Tudum", "sourceUrl": "https://www.netflix.com/example",
               "announcementDate": None, "lastChecked": "2026-09-01"}
        release_year = {"tmdbId": 500001, "title": "Example", "nextSeasonNumber": 3,
                        "status": "RENEWED", "releaseDate": None, "releaseYear": 2027,
                        "sourceName": "Netflix Tudum", "sourceUrl": "https://www.netflix.com/year",
                        "announcementDate": None, "rule": "explicit-release-year"}
        updated, evidence = update.merge(old, [release_year], self.now.date())
        self.assertEqual(updated["releaseYear"], 2027)
        self.assertIsNotNone(evidence)
        date_fact = {**release_year, "status": "RELEASE_DATE_CONFIRMED", "releaseDate": "2027-08-01",
                     "releaseYear": 2027, "rule": "explicit-premiere"}
        updated, _ = update.merge(old, [date_fact], self.now.date())
        self.assertEqual((updated["status"], updated["releaseDate"]), ("RENEWED", "2027-08-01"))
        final = {**old, "status": "FINAL_SEASON"}
        updated, _ = update.merge(final, [date_fact], self.now.date())
        self.assertEqual((updated["status"], updated["releaseDate"]), ("FINAL_SEASON", "2027-08-01"))
        confirmed = {**old, "status": "RELEASE_DATE_CONFIRMED", "releaseDate": "2027-08-01", "releaseYear": 2027}
        renewal = {**release_year, "rule": "explicit-renewal", "releaseYear": None}
        updated, _ = update.merge(confirmed, [renewal], self.now.date())
        self.assertEqual(updated["status"], "RELEASE_DATE_CONFIRMED")
        self.assertEqual(updated["releaseDate"], "2027-08-01")
        self.assertEqual(updated["releaseYear"], 2027)

    def test_same_season_year_only_conflict_is_rejected(self):
        old = {"tmdbId": 500001, "title": "Example", "nextSeasonNumber": 3,
               "status": "RENEWED", "releaseDate": None, "releaseYear": 2027,
               "sourceName": "Netflix Tudum", "sourceUrl": "https://www.netflix.com/example",
               "announcementDate": None, "lastChecked": "2026-09-01"}
        incoming = {"tmdbId": 500001, "title": "Example", "nextSeasonNumber": 3,
                    "status": "RENEWED", "releaseDate": None, "releaseYear": 2028,
                    "sourceName": "Netflix Tudum", "sourceUrl": "https://www.netflix.com/year",
                    "announcementDate": None, "rule": "explicit-release-year"}
        with self.assertRaisesRegex(update.AutomationError, "Conflicting release year"):
            update.merge(old, [incoming], self.now.date())

    def test_newer_season_and_explicit_cancellation_merge(self):
        old = {"tmdbId": 500001, "title": "Example", "nextSeasonNumber": 2,
               "status": "RENEWED", "releaseDate": None, "releaseYear": None,
               "sourceName": "Netflix Tudum", "sourceUrl": "https://www.netflix.com/example",
               "announcementDate": None, "lastChecked": "2026-09-01"}
        newer = {"tmdbId": 500001, "title": "Example", "nextSeasonNumber": 3,
                 "status": "RENEWED", "releaseDate": None, "releaseYear": None,
                 "sourceName": "Netflix Tudum", "sourceUrl": "https://www.netflix.com/s3",
                 "announcementDate": None, "rule": "explicit-renewal"}
        older = {**newer, "nextSeasonNumber": 1, "sourceUrl": "https://www.netflix.com/s1"}
        self.assertEqual(update.merge(old, [older], self.now.date()), (old, None))
        self.assertEqual(update.merge(old, [newer], self.now.date())[0]["nextSeasonNumber"], 3)
        canceled = {**newer, "status": "CANCELED", "rule": "explicit-cancellation"}
        result, _ = update.merge(old, [canceled], self.now.date())
        self.assertEqual(result["status"], "CANCELED")

    def test_failed_canonical_replace_rolls_back_audit(self):
        before_data = self.data_path.read_bytes()
        before_audit = self.audit_path.read_bytes()
        original_replace = update.os.replace
        failed = False
        def replace(source, target):
            nonlocal failed
            if Path(target) == self.data_path and not failed:
                failed = True
                raise OSError("simulated canonical write failure")
            return original_replace(source, target)
        with patch.object(update.os, "replace", side_effect=replace):
            result = self.promote(dry_run=False)
        self.assertEqual(result["promotionState"], "VALIDATION_FAILED")
        self.assertEqual(before_data, self.data_path.read_bytes())
        self.assertEqual(before_audit, self.audit_path.read_bytes())

    def test_failed_audit_replace_leaves_canonical_unchanged(self):
        before_data = self.data_path.read_bytes()
        before_audit = self.audit_path.read_bytes()
        original_replace = update.os.replace
        def replace(source, target):
            if Path(target) == self.audit_path:
                raise OSError("simulated audit write failure")
            return original_replace(source, target)
        with patch.object(update.os, "replace", side_effect=replace):
            result = self.promote(dry_run=False)
        self.assertEqual(result["promotionState"], "VALIDATION_FAILED")
        self.assertEqual(before_data, self.data_path.read_bytes())
        self.assertEqual(before_audit, self.audit_path.read_bytes())

    def test_verification_failure_restores_both_files(self):
        before_data = self.data_path.read_bytes()
        before_audit = self.audit_path.read_bytes()
        validator = update.validate_dataset
        calls = 0
        def fail_after_replace(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise update.AutomationError("simulated post-write verification failure")
            return validator(*args, **kwargs)
        with patch.object(update, "validate_dataset", side_effect=fail_after_replace):
            result = self.promote(dry_run=False)
        self.assertEqual(result["promotionState"], "VALIDATION_FAILED")
        self.assertEqual(before_data, self.data_path.read_bytes())
        self.assertEqual(before_audit, self.audit_path.read_bytes())

    def test_daily_validator_preserves_promoted_supplemental_records(self):
        result = self.promote(dry_run=False)
        self.assertEqual(result["promotionState"], "PROMOTED")
        promoted = json.loads(self.data_path.read_text(encoding="utf-8"))
        update.validate_dataset(promoted, self.registry, self.production,
                                allow_supplemental_ids={111110})
        next_run = copy.deepcopy(promoted)
        update.validate_dataset(next_run, self.registry, promoted)
        self.assertIn(111110, {row["tmdbId"] for row in next_run["series"]})
        with patch.object(update, "DATA", self.data_path), \
             patch.object(update, "REGISTRY", self.registry_path), \
             patch.object(update, "AUDIT", self.audit_path):
            daily = update.run(True, collector=lambda *_: ([], 0), today=self.now.date())
        self.assertTrue(daily["publishable"], daily["failures"])
        self.assertIn(111110, {row["tmdbId"] for row in json.loads(self.data_path.read_bytes())["series"]})


if __name__ == "__main__":
    unittest.main()
