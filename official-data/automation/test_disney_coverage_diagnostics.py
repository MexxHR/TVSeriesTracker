"""Offline fixtures for the read-only Disney+ coverage diagnostic."""
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))

import disney_coverage_diagnostics as diag
import discovery
import monitored
import promotion
import update


META = {"tmdbId": 103540, "title": "Percy Jackson and the Olympians",
        "originalName": "Percy Jackson and the Olympians",
        "networks": [{"name": "Disney+"}], "productionCompanies": [],
        "homepage": None, "originCountry": []}
ARTICLE = """<html><head><title>Percy Jackson and the Olympians renewed for Season 3</title>
<meta property="article:published_time" content="2026-01-20"></head>
<body><h1>Percy Jackson and the Olympians renewed for Season 3</h1>
<article><p>Percy Jackson and the Olympians has been renewed for a third season.
The production announcement confirms the series will return with its original cast.
Disney+ shared the update with fans and production will begin later this year.</p></article>
</body></html>"""
URL = "https://press.disneyplus.com/news/percy-jackson-and-the-olympians-season-3-renewed"
SITEMAP = ("<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
           f"<url><loc>{URL}</loc></url></urlset>")


class DisneyCoverageDiagnosticsTest(unittest.TestCase):
    def fetch_fixture(self, url, domains, headers=None):
        self.assertEqual(domains, ["press.disneyplus.com"])
        if url == "https://press.disneyplus.com/sitemap.xml":
            return SITEMAP, url
        if url == URL:
            return ARTICLE, url
        raise update.ProviderUnavailable("HTTP 404: fixture")

    def test_positive_control_uses_real_production_discovery_and_monitored_parser(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            registry = Path(directory) / "monitored.json"
            audit = Path(directory) / "monitored.jsonl"
            registry.write_text('{"schemaVersion":1,"series":[]}\n', encoding="utf-8")
            audit.write_bytes(b"")
            with patch.object(monitored, "MONITORED", registry), \
                 patch.object(monitored, "MONITORED_AUDIT", audit):
                case = diag.diagnose_case(103540, META, fetcher=self.fetch_fixture)
            self.assertEqual(case["routing"]["provider"], "DISNEY_PLUS")
            self.assertEqual(case["pipeline"]["discoveryResult"], "candidate_found")
            self.assertEqual(case["pipeline"]["pipelineState"], "VERIFIED_FACTS")
            self.assertEqual(case["parser"]["result"], "facts")
            self.assertTrue(case["pipeline"]["dryRun"])
            self.assertEqual(json.loads(registry.read_text(encoding="utf-8"))["series"], [])

    def test_diagnostic_only_url_never_enters_monitored_discovery_input(self):
        urls = [f"https://press.disneyplus.com/news/percy-jackson-and-the-olympians-season-{n}"
                for n in range(1, 22)]
        fake_url = urls[-1]
        metas = []

        def processor(tmdb_id, *, dry_run, metadata, discoverer, fetcher):
            metas.append(metadata)
            prod = discoverer(metadata, {"series": []})
            self.assertNotIn(fake_url, prod["candidateUrls"])
            return {"tmdbId": tmdb_id, "dryRun": True, "discoveryResult": "insufficient_evidence",
                    "pipelineState": "DISCOVERY_INSUFFICIENT", "parserResult": "skipped",
                    "validationResult": "not_applicable", "candidateUrls": [],
                    "eligibleCandidateCount": 0, "candidateDiagnostics": []}

        xml = ("<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>" +
               "".join(f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>")

        def fetcher(url, domains, headers=None):
            if url.endswith("sitemap.xml"):
                return xml, url
            if url == fake_url:
                return ARTICLE, url
            raise update.ProviderUnavailable("HTTP 404")

        case = diag.diagnose_case(103540, META, fetcher=fetcher, processor=processor)
        only = next(item for item in case["diagnosticDiscovery"]["relevantUrls"] if item["url"] == fake_url)
        self.assertTrue(only["diagnosticOnlyObservation"])
        self.assertFalse(only["productionConsidered"])
        self.assertEqual(metas, [META])

    def test_non_disney_routing_stops_before_processor_or_official_fetch(self):
        other = {**META, "networks": [{"name": "Disney Channel"}]}
        def forbidden(*args, **kwargs):
            self.fail("Factual Disney+ processing should stop after routing")
        case = diag.diagnose_case(103540, other, fetcher=forbidden, processor=forbidden)
        self.assertEqual(case["classification"], "ROUTING_NOT_DISNEY_PLUS")
        self.assertTrue(case["routing"]["factualProcessingStopped"])

    def test_url_absent_and_filtered_identity_are_distinct(self):
        absent = {"routing": {"provider": "DISNEY_PLUS"}, "pipeline": {"pipelineState": "DISCOVERY_INSUFFICIENT"},
                  "identityChecks": [], "structureChecks": [],
                  "diagnosticDiscovery": {"status": "completed", "relevantUrls": []}}
        self.assertEqual(diag.classify_case(absent), "NO_RELEVANT_OFFICIAL_URL")
        rejected = {**absent, "identityChecks": [{"seriesIdentityConfirmed": False}]}
        self.assertEqual(diag.classify_case(rejected), "IDENTITY_BINDING_REJECTED")
        filtered = {**absent, "diagnosticDiscovery": {"status": "completed", "relevantUrls": [
            {"productionConsidered": False, "productionArticleLinkMatch": False,
             "positionOutsideProductionArticleLimit": False}]}}
        self.assertEqual(diag.classify_case(filtered), "URL_FILTERED_BEFORE_FETCH")
        unselected = {**absent, "diagnosticDiscovery": {"status": "completed", "relevantUrls": [
            {"productionConsidered": False, "productionArticleLinkMatch": True,
             "positionOutsideProductionArticleLimit": False}]}}
        self.assertEqual(diag.classify_case(unselected), "URL_PRESENT_NOT_SELECTED_BY_PRODUCTION")

    def test_outside_current_bounds_is_observable(self):
        many = [f"<url><loc>https://press.disneyplus.com/news/percy-jackson-and-the-olympians-story-{n}</loc></url>"
                for n in range(25)]
        xml = "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>" + "".join(many) + "</urlset>"
        calls = []
        def fetcher(url, domains, headers=None):
            calls.append((url, domains))
            return (xml if url.endswith("sitemap.xml") else ARTICLE), url
        result = diag._deep_sitemap_inspection(META, [], fetcher)
        self.assertEqual(len(result["relevantUrls"]), 25)
        self.assertTrue(result["relevantUrls"][20]["positionOutsideProductionArticleLimit"])
        self.assertTrue(all(domains == ["press.disneyplus.com"] for _, domains in calls))
        self.assertLessEqual(len(calls), diag.DIAGNOSTIC_LIMITS["maxTotalFetches"])

    def test_production_article_rank_uses_exact_token_filter_and_deduplicates(self):
        xmen = {**META, "tmdbId": 138502, "title": "X-Men '97",
                "originalName": "X-Men '97"}
        urls = [f"https://press.disneyplus.com/news/men-story-{n}" for n in range(20)]
        urls.append("https://press.disneyplus.com/news/x-men-97-launch-event")
        xml = ("<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>" +
               "".join(f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>")
        result = diag._deep_sitemap_inspection(xmen, [],
            lambda url, domains: (xml if url.endswith("sitemap.xml") else ARTICLE, url))
        target = next(item for item in result["relevantUrls"] if item["url"] == urls[-1])
        self.assertEqual(target["productionMatchingUrlIndex"], 21)
        self.assertTrue(target["positionOutsideProductionArticleLimit"])

    def test_production_sitemap_failure_takes_precedence_over_diagnostic_only_article(self):
        case = {"routing": {"provider": "DISNEY_PLUS"},
                "pipeline": {"pipelineState": "DISCOVERY_INSUFFICIENT"},
                "productionDiscovery": {"sitemaps": [{"fetchResult": "success",
                    "productionParseResult": "url_entry_limit_exceeded"}]},
                "identityChecks": [{"seriesIdentityConfirmed": False}],
                "structureChecks": [], "diagnosticDiscovery": {"relevantUrls": []}}
        self.assertEqual(diag.classify_case(case), "PRODUCTION_SITEMAP_LIMIT_OR_PARSE_FAILURE")

    def test_unexpected_diagnostic_fetch_error_propagates(self):
        def broken(*args, **kwargs):
            raise RuntimeError("programming fault")
        with self.assertRaises(RuntimeError):
            diag._deep_sitemap_inspection(META, [], broken)

    def test_identity_and_article_structure_rejection_are_separate(self):
        nonmatch = "<html><head><title>Official news</title></head><body><h1>Launch event in Hollywood</h1><p>" + "x" * 100 + "</p></body></html>"
        captures = [{"stage": "production_discovery", "requestedUrl": URL, "finalUrl": URL,
                     "fetchResult": "success", "raw": nonmatch}]
        ids, structures, _ = diag._captured_page_checks(META, captures,
            {"candidates": [], "rejectedCandidates": [{"url": URL, "reasons": ["series_identity_not_confirmed", "article_structure_not_confirmed"]}]})
        self.assertFalse(ids[0]["seriesIdentityConfirmed"])
        self.assertEqual(ids[0]["rejectionReasons"], ["series_identity_not_confirmed"])
        self.assertFalse(structures[0]["articleStructureConfirmed"])
        self.assertEqual(structures[0]["rejectionReasons"], ["article_structure_not_confirmed"])

    def test_parser_no_facts_classification_is_distinct(self):
        case = {"routing": {"provider": "DISNEY_PLUS"},
                "pipeline": {"pipelineState": "NO_VERIFIED_FACTS"}}
        self.assertEqual(diag.classify_case(case), "PARSER_NO_FACTS")

    def test_verified_facts_trigger_reconstruction_and_mismatch_is_reported(self):
        fact = {"status": "RENEWED", "nextSeasonNumber": 3, "releaseDate": None,
                "releaseYear": None, "sourceName": "Disney+ Press", "sourceUrl": URL,
                "announcementDate": None}
        row = {"tmdbId": 103540, "lastChecked": "2026-10-10", "verifiedFacts": fact}

        def processor(tmdb_id, *, dry_run, metadata, discoverer, fetcher):
            discoverer(metadata, {"series": []})
            return {"tmdbId": tmdb_id, "dryRun": True, "pipelineState": "VERIFIED_FACTS",
                    "discoveryResult": "candidate_found", "eligibleCandidateCount": 1,
                    "candidateUrls": [URL], "verifiedFacts": fact, "proposedRecord": row,
                    "parserResult": "facts", "validationResult": "passed"}

        with patch.object(promotion, "_validate_evidence", return_value=[{}]), \
             patch.object(promotion, "_facts_summary", return_value={**fact, "releaseYear": 2027}):
            case = diag.diagnose_case(103540, META, fetcher=self.fetch_fixture, processor=processor)
        self.assertEqual(case["independentReconstruction"]["result"], "failed")
        self.assertIn("releaseYear", case["independentReconstruction"]["mismatchedFields"])
        self.assertEqual(case["classification"], "INDEPENDENT_RECONSTRUCTION_MISMATCH")

    def test_sitemap_parser_rejects_entities_and_unsupported_roots(self):
        self.assertEqual(diag._parse_sitemap("<!DOCTYPE x [<!ENTITY y 'z'>]><x/>")[1], "doctype_or_entity_rejected")
        self.assertEqual(diag._parse_sitemap("<html/>")[1], "unsupported_sitemap_root")
        self.assertEqual(diag._parse_sitemap("<broken")[1], "xml_parse_failed")

    def test_deep_scan_never_fetches_unapproved_sitemap_entries(self):
        malicious = "https://example.com/news/percy-jackson-and-the-olympians-season-3"
        xml = ("<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
               f"<url><loc>{malicious}</loc></url></urlset>")
        calls = []
        def fetcher(url, domains):
            calls.append((url, domains))
            return xml, url
        result = diag._deep_sitemap_inspection(META, [], fetcher)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["relevantUrls"], [])

    def test_matching_first_party_url_with_wrong_path_is_reported_filtered(self):
        filtered_url = "https://press.disneyplus.com/press/percy-jackson-and-the-olympians-season-3"
        xml = ("<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
               f"<url><loc>{filtered_url}</loc></url></urlset>")
        result = diag._deep_sitemap_inspection(META, [],
            lambda url, domains: (xml, url))
        self.assertEqual(result["filteredUrlCount"], 1)
        self.assertEqual(result["filteredUrls"][0]["exclusionReason"], "production_article_path_filter")

    def test_main_fails_if_protected_hash_or_reconstruction_gate_fails(self):
        failed = {"protectedFileIntegrity": {"unchanged": False}, "diagnosticErrors": [], "cases": []}
        with patch.object(diag, "run", return_value=failed), \
             patch.object(diag, "_write_report", return_value=Path("unused")):
            with self.assertRaises(SystemExit):
                diag.main()
        mismatch = {"protectedFileIntegrity": {"unchanged": True}, "diagnosticErrors": [],
                    "cases": [{"independentReconstruction": {"result": "failed"}}]}
        with patch.object(diag, "run", return_value=mismatch), \
             patch.object(diag, "_write_report", return_value=Path("unused")):
            with self.assertRaises(SystemExit):
                diag.main()


if __name__ == "__main__":
    unittest.main()
