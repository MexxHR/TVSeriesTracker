import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import monitored as m
import update as u


class MonitoredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "monitored.json"
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        self.meta = {"tmdbId": 111110, "title": "One Piece", "networks": [{"name": "Netflix"}],
                     "homepage": "https://www.netflix.com/title/80217863"}
        self.article = "https://www.netflix.com/tudum/articles/one-piece-renewed-season-3"
        self.index = "https://www.netflix.com/tudum/one-piece"
        self.today = dt.date(2026, 9, 28)
        self.now = dt.datetime(2026, 9, 28, 12, tzinfo=dt.timezone.utc)

    def candidate(self, url=None, eligible=True):
        return {"candidateUrl": url or self.article, "factualParserEligible": eligible,
                "evidenceLevel": "STRONG" if eligible else "SUPPORTED"}

    def report(self, candidates=None, result="candidate_found", provider="NETFLIX", reason=None):
        candidates = candidates if candidates is not None else [self.candidate()]
        return {"provider": provider, "result": result, "reason": reason,
                "candidateUrls": [c["candidateUrl"] for c in candidates], "candidates": candidates}

    def run_process(self, html=None, *, report=None, dry=True, meta=None, fetcher=None):
        report = report if report is not None else self.report()
        if fetcher is None:
            def fetcher(url, domains):
                self.assertIn("netflix.com", domains)
                return html if html is not None else '<h1>One Piece Season 3</h1><p>One Piece was renewed for Season 3.</p>', url
        return m.process((meta or self.meta)["tmdbId"], dry_run=dry, metadata=meta or self.meta,
                         discoverer=lambda *_: report, fetcher=fetcher, today=self.today, now=self.now,
                         registry_path=self.path, audit_path=self.audit)

    def test_eligible_candidate_reaches_parser(self):
        result = self.run_process()
        self.assertEqual((result["pipelineState"], result["parsedSourceCount"]), ("VERIFIED_FACTS", 1))

    def test_noneligible_never_reaches_parser(self):
        result = self.run_process(report=self.report([self.candidate(eligible=False)]),
                                  fetcher=lambda *_: self.fail("parser fetch must not run"))
        self.assertEqual((result["pipelineState"], result["parsedSourceCount"]), ("NOT_PARSER_ELIGIBLE", 0))

    def test_mixed_candidates_fetch_only_eligible(self):
        seen = []
        def fetch(url, domains):
            seen.append(url)
            return '<h1>One Piece Season 3</h1><p>One Piece was renewed for Season 3.</p>', url
        report = self.report([self.candidate(self.index, False), self.candidate()])
        result = self.run_process(report=report, fetcher=fetch)
        self.assertEqual(seen, [self.article])
        self.assertEqual(result["eligibleCandidateCount"], 1)

    def test_zero_eligible_is_not_parser_eligible(self):
        result = self.run_process(report=self.report([]), fetcher=lambda *_: self.fail("fetch"))
        self.assertEqual(result["pipelineState"], "NOT_PARSER_ELIGIBLE")

    def test_explicit_renewal(self):
        result = self.run_process()
        self.assertEqual(result["verifiedFacts"]["status"], "RENEWED")
        self.assertEqual(result["verifiedFacts"]["nextSeasonNumber"], 3)

    def test_explicit_final_season(self):
        result = self.run_process('<h1>One Piece Season 4</h1><p>One Piece Season 4 is the final season.</p>')
        self.assertEqual(result["verifiedFacts"]["status"], "FINAL_SEASON")

    def test_explicit_cancellation(self):
        result = self.run_process('<h1>One Piece Season 4</h1><p>One Piece Season 4 was canceled.</p>')
        self.assertEqual(result["verifiedFacts"]["status"], "CANCELED")

    def test_explicit_premiere_date(self):
        result = self.run_process('<h1>One Piece Season 3</h1><p>One Piece Season 3 premieres August 2, 2027.</p>')
        self.assertEqual(result["verifiedFacts"]["status"], "RELEASE_DATE_CONFIRMED")
        self.assertEqual(result["verifiedFacts"]["releaseDate"], "2027-08-02")

    def test_renewal_and_later_premiere(self):
        a = self.article
        b = "https://www.netflix.com/tudum/articles/one-piece-premiere"
        pages = {a: '<h1>One Piece Season 3</h1><p>One Piece was renewed for Season 3.</p>',
                 b: '<h1>One Piece Season 3</h1><p>One Piece Season 3 premieres August 2, 2027.</p>'}
        result = self.run_process(report=self.report([self.candidate(a), self.candidate(b)]),
                                  fetcher=lambda url, _: (pages[url], url))
        self.assertEqual(result["verifiedFacts"]["status"], "RELEASE_DATE_CONFIRMED")
        self.assertEqual(result["verifiedFacts"]["sourceUrl"], b)
        self.assertEqual(len({f["sourceUrl"] for f in result["sourceEvidence"]}), 2)

    def test_final_season_and_premiere_preserves_final(self):
        a = self.article
        b = "https://www.netflix.com/tudum/articles/one-piece-premiere"
        pages = {a: '<h1>One Piece Season 4</h1><p>One Piece Season 4 is the final season.</p>',
                 b: '<h1>One Piece Season 4</h1><p>One Piece Season 4 premieres August 2, 2027.</p>'}
        result = self.run_process(report=self.report([self.candidate(a), self.candidate(b)]),
                                  fetcher=lambda url, _: (pages[url], url))
        self.assertEqual(result["verifiedFacts"]["status"], "FINAL_SEASON")
        self.assertEqual(result["verifiedFacts"]["releaseDate"], "2027-08-02")

    def test_newer_season_precedence(self):
        a = self.article
        b = "https://www.netflix.com/tudum/articles/one-piece-season-4"
        pages = {a: '<h1>One Piece Season 3</h1><p>One Piece was renewed for Season 3.</p>',
                 b: '<h1>One Piece Season 4</h1><p>One Piece was renewed for Season 4.</p>'}
        result = self.run_process(report=self.report([self.candidate(a), self.candidate(b)]),
                                  fetcher=lambda url, _: (pages[url], url))
        self.assertEqual(result["verifiedFacts"]["nextSeasonNumber"], 4)

    def test_url_slug_alone_is_not_fact(self):
        result = self.run_process('<h1>One Piece News</h1><p>One Piece cast interviews.</p>')
        self.assertEqual(result["pipelineState"], "NO_VERIFIED_FACTS")

    def test_title_collision_is_not_fact(self):
        result = self.run_process('<h1>The One Piece Season 3</h1><p>The One Piece was renewed for Season 3.</p>')
        self.assertEqual(result["pipelineState"], "VALIDATION_FAILED")

    def test_other_show_mentioned_on_valid_page_is_not_fact(self):
        result = self.run_process('<h1>One Piece News</h1><p>The One Piece was renewed for Season 3.</p>')
        self.assertEqual(result["pipelineState"], "NO_VERIFIED_FACTS")

    def test_wrong_series_is_not_fact(self):
        result = self.run_process('<h1>Wednesday Season 3</h1><p>Wednesday was renewed for Season 3.</p>')
        self.assertEqual(result["pipelineState"], "VALIDATION_FAILED")

    def test_generic_official_page_is_not_fact(self):
        result = self.run_process('<h1>One Piece</h1><p>Watch now on Netflix.</p>')
        self.assertEqual(result["pipelineState"], "NO_VERIFIED_FACTS")

    def test_unsupported_status_wording_is_not_fact(self):
        result = self.run_process('<h1>One Piece</h1><p>One Piece Season 3 might happen.</p>')
        self.assertEqual(result["pipelineState"], "NO_VERIFIED_FACTS")

    def test_ambiguous_season_is_not_fact(self):
        result = self.run_process('<h1>One Piece</h1><p>One Piece was renewed for more episodes.</p>')
        self.assertEqual(result["pipelineState"], "NO_VERIFIED_FACTS")

    def test_conflicting_dates_fail_validation(self):
        a, b = self.article, "https://www.netflix.com/tudum/articles/one-piece-date"
        pages = {a: '<h1>One Piece Season 3</h1><p>One Piece Season 3 premieres August 2, 2027.</p>',
                 b: '<h1>One Piece Season 3</h1><p>One Piece Season 3 premieres August 3, 2027.</p>'}
        result = self.run_process(report=self.report([self.candidate(a), self.candidate(b)]),
                                  fetcher=lambda url, _: (pages[url], url))
        self.assertEqual(result["pipelineState"], "VALIDATION_FAILED")
        self.assertEqual(result["verifiedFacts"], {})

    def test_conflicting_cancellation_and_renewal_fail_validation(self):
        a, b = self.article, "https://www.netflix.com/tudum/articles/one-piece-canceled"
        pages = {a: '<h1>One Piece Season 3</h1><p>One Piece was renewed for Season 3.</p>',
                 b: '<h1>One Piece Season 3</h1><p>One Piece Season 3 was canceled.</p>'}
        result = self.run_process(report=self.report([self.candidate(a), self.candidate(b)]),
                                  fetcher=lambda url, _: (pages[url], url))
        self.assertEqual(result["pipelineState"], "VALIDATION_FAILED")

    def test_validation_failure_never_writes_live_registry(self):
        result = self.run_process('<h1>The One Piece Season 3</h1><p>The One Piece was renewed for Season 3.</p>', dry=False)
        self.assertEqual(result["registryChange"], "none")
        self.assertFalse(self.path.exists())
        self.assertFalse(self.audit.exists())

    def test_malformed_date_not_verified(self):
        result = self.run_process('<h1>One Piece Season 3</h1><p>One Piece Season 3 premieres February 30, 2027.</p>')
        self.assertEqual(result["pipelineState"], "NO_VERIFIED_FACTS")

    def test_production_only_is_not_renewal(self):
        result = self.run_process('<h1>One Piece Season 3</h1><p>One Piece Season 3 began production.</p>')
        self.assertEqual(result["pipelineState"], "NO_VERIFIED_FACTS")

    def test_extended_final_rule_does_not_change_production_default(self):
        article = u.ADAPTERS["NETFLIX"].parse('<h1>One Piece Season 4</h1><p>One Piece Season 4 is the final season.</p>', self.article)
        entry = {"tmdbId": 111110, "title": "One Piece", "aliases": ["One Piece"]}
        self.assertEqual(u.detect(article, entry, self.today), [])
        self.assertEqual(u.detect(article, entry, self.today, extended_final=True)[0]["status"], "FINAL_SEASON")

    def test_provider_unavailable_after_discovery(self):
        result = self.run_process(fetcher=lambda *_: (_ for _ in ()).throw(u.ProviderUnavailable("HTTP 403")))
        self.assertEqual(result["pipelineState"], "PROVIDER_UNAVAILABLE")

    def test_wbd_403_discovery_never_fetches_parser(self):
        report = self.report([], "provider_unavailable", "WBD")
        result = self.run_process(report=report, fetcher=lambda *_: self.fail("parser"))
        self.assertEqual(result["pipelineState"], "PROVIDER_UNAVAILABLE")

    def test_dry_run_no_files_written(self):
        result = self.run_process()
        self.assertEqual(result["registryChange"], "would_add")
        self.assertIsNotNone(result["proposedRecord"])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.audit.exists())

    def test_live_add_and_audit(self):
        result = self.run_process(dry=False)
        self.assertEqual(result["registryChange"], "added")
        self.assertEqual(len(json.loads(self.path.read_text())["series"]), 1)
        self.assertEqual(len(self.audit.read_text().splitlines()), 1)

    def test_live_update_and_audit(self):
        self.run_process(dry=False)
        result = self.run_process('<h1>One Piece Season 4</h1><p>One Piece was renewed for Season 4.</p>', dry=False)
        self.assertEqual(result["registryChange"], "updated")
        self.assertEqual(len(self.audit.read_text().splitlines()), 2)

    def test_noop_does_not_refresh_timestamp_or_audit(self):
        self.run_process(dry=False)
        before = self.path.read_bytes(), self.audit.read_bytes()
        result = self.run_process(dry=False)
        self.assertEqual(result["registryChange"], "none")
        self.assertEqual(before, (self.path.read_bytes(), self.audit.read_bytes()))

    def test_atomic_rollback_on_audit_replace_failure(self):
        self.run_process(dry=False)
        before = self.path.read_bytes(), self.audit.read_bytes()
        original_replace = m.os.replace
        failed = False
        def replace(src, dst):
            nonlocal failed
            if Path(dst) == self.audit and not failed:
                failed = True
                raise OSError("simulated audit write failure")
            return original_replace(src, dst)
        with patch.object(m.os, "replace", side_effect=replace), self.assertRaises(OSError):
            self.run_process('<h1>One Piece Season 4</h1><p>One Piece was renewed for Season 4.</p>', dry=False)
        self.assertEqual(before, (self.path.read_bytes(), self.audit.read_bytes()))

    def test_duplicate_tmdb_id_rejected(self):
        self.run_process(dry=False)
        data = json.loads(self.path.read_text())
        data["series"].append(data["series"][0])
        with self.assertRaisesRegex(u.AutomationError, "Duplicate"):
            m.validate_registry(data)

    def test_deterministic_ordering(self):
        self.run_process(dry=False)
        meta = {"tmdbId": 95396, "title": "Severance", "networks": [{"name": "Apple TV"}]}
        report = self.report([], "insufficient_evidence", "APPLE")
        self.run_process(report=report, meta=meta, dry=False, fetcher=lambda *_: self.fail("parser"))
        self.assertEqual([r["tmdbId"] for r in json.loads(self.path.read_text())["series"]], [95396, 111110])

    def test_schema_validation_rejects_fake_domain(self):
        self.run_process(dry=False)
        data = json.loads(self.path.read_text())
        data["series"][0]["candidateUrls"] = ["https://netflix.com.evil.example/tudum/one-piece"]
        with self.assertRaisesRegex(u.AutomationError, "Untrusted"):
            m.validate_registry(data)

    def test_old_schema_v1_evidence_without_optional_text_remains_valid(self):
        self.run_process(dry=False)
        data = json.loads(self.path.read_text())
        for fact in data["series"][0]["sourceEvidence"]:
            fact.pop("evidenceText", None)
            fact.pop("factType", None)
        m.validate_registry(data)

    def test_oversized_evidence_excerpt_rejected(self):
        self.run_process(dry=False)
        data = json.loads(self.path.read_text())
        data["series"][0]["sourceEvidence"][0]["evidenceText"] = "x" * 201
        with self.assertRaisesRegex(u.AutomationError, "excerpt"):
            m.validate_registry(data)

    def test_trusted_id_precedence(self):
        result = m.process(153312, metadata={"tmdbId": 153312}, dry_run=False,
                           discoverer=lambda *_: self.fail("discovery"), registry_path=self.path, audit_path=self.audit)
        self.assertEqual(result["pipelineState"], "ALREADY_TRUSTED")
        self.assertFalse(self.path.exists())

    def test_metadata_unavailable(self):
        with patch.object(m.discovery, "tmdb_metadata", side_effect=ValueError("missing token")):
            result = m.process(111110, metadata=None, dry_run=True, registry_path=self.path, audit_path=self.audit)
        self.assertEqual(result["pipelineState"], "METADATA_UNAVAILABLE")

    def test_production_files_unchanged(self):
        paths = [u.DATA, u.REGISTRY, u.AUDIT]
        before = [p.read_bytes() for p in paths]
        self.run_process(dry=False)
        self.assertEqual(before, [p.read_bytes() for p in paths])


if __name__ == "__main__":
    unittest.main()
