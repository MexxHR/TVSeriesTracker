import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import discovery as d
import update as u


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.registry = json.loads(u.REGISTRY.read_text(encoding="utf-8"))
        self.specs = d.provider_specs()
        self.meta = {"tmdbId": 111110, "title": "One Piece", "originalName": "ONE PIECE",
                     "networks": [{"name": "Netflix"}], "productionCompanies": [],
                     "homepage": "https://www.netflix.com/title/80217863"}
        self.index = "https://www.netflix.com/tudum/one-piece"

    def fake(self, pages):
        def fetch(url, domains):
            item = pages.get(url)
            if isinstance(item, BaseException):
                raise item
            if item is None:
                raise u.ProviderUnavailable("HTTP 404")
            return item if isinstance(item, tuple) else (item, url)
        return fetch

    def discover(self, pages, metadata=None):
        return d.discover(metadata or self.meta, self.registry, self.fake(pages), self.specs)

    def test_central_registry_has_five_providers(self):
        self.assertEqual(set(self.specs), {"PARAMOUNT", "NETFLIX", "APPLE", "AMAZON", "WBD"})

    def test_provider_registry_rejects_unofficial_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            spec = json.loads(d.PROVIDERS.read_text())
            spec["NETFLIX"]["seeds"] = ["https://netflix.com.evil.example/tudum/{slug}"]
            path.write_text(json.dumps(spec))
            with patch.object(d, "PROVIDERS", path), self.assertRaisesRegex(ValueError, "Invalid discovery seed"):
                d.provider_specs()

    def test_netflix_domain_boundary(self):
        self.assertTrue(u.allowed("https://foo.netflix.com/tudum/test", self.specs["NETFLIX"]["officialDomains"]))
        self.assertFalse(u.allowed("https://netflix.com.evil.example/test", self.specs["NETFLIX"]["officialDomains"]))

    def test_paramount_domain_boundary(self):
        self.assertFalse(u.allowed("https://paramountpressexpress.com.fake.example/test", self.specs["PARAMOUNT"]["officialDomains"]))

    def test_routes_each_existing_provider(self):
        for label, provider in (("Netflix", "NETFLIX"), ("Apple TV", "APPLE"), ("Paramount+", "PARAMOUNT"),
                                ("Prime Video", "AMAZON"), ("HBO", "WBD")):
            with self.subTest(label=label):
                meta = {**self.meta, "homepage": None, "networks": [{"name": label}]}
                self.assertEqual(d.route_provider(meta, self.specs)[0], provider)

    def test_missing_provider_insufficient(self):
        report = self.discover({}, {"tmdbId": 900001, "title": "Unknown"})
        self.assertEqual((report["result"], report["reason"]), ("insufficient_evidence", "unknown_provider"))

    def test_unknown_network_insufficient(self):
        meta = {**self.meta, "homepage": "", "networks": [{"name": "Unknown TV"}]}
        self.assertEqual(self.discover({}, meta)["reason"], "unknown_provider")

    def test_conflicting_network_and_homepage_insufficient(self):
        meta = {**self.meta, "homepage": "https://tv.apple.com/show/one-piece"}
        self.assertEqual(self.discover({}, meta)["reason"], "ambiguous_provider")

    def test_homepage_routes_but_is_not_evidence(self):
        meta = {**self.meta, "networks": []}
        report = self.discover({self.index: "<h1>One Piece</h1><p>News</p>"}, meta)
        self.assertEqual(report["provider"], "NETFLIX")
        self.assertEqual(report["evidenceLevel"], "SUPPORTED")
        self.assertFalse(report["candidates"][0]["factualParserEligible"])

    def test_official_index_exact_title_strong(self):
        page = '<h1>One Piece</h1><p>News and featured articles</p><a href="https://www.netflix.com/watch/80217863">Watch</a>'
        report = self.discover({self.index: page})
        self.assertEqual((report["result"], report["evidenceLevel"]), ("candidate_found", "STRONG"))
        self.assertEqual(report["candidateUrls"], [self.index])
        self.assertTrue(report["candidates"][0]["factualParserEligible"])
        self.assertTrue(report["candidates"][0]["homepageIdMatched"])

    def test_same_title_without_unique_homepage_link_is_only_supported(self):
        report = self.discover({self.index: "<h1>One Piece</h1><p>News</p>"})
        self.assertEqual(report["evidenceLevel"], "SUPPORTED")
        self.assertFalse(report["candidates"][0]["factualParserEligible"])

    def test_no_factual_status_from_tmdb(self):
        meta = {**self.meta, "status": "Canceled", "next_episode_to_air": {"air_date": "2027-01-01"}}
        report = self.discover({self.index: "<h1>One Piece</h1><p>News</p>"}, meta)
        self.assertNotIn("status", report)
        self.assertNotIn("releaseDate", report)

    def test_unofficial_article_even_with_exact_title_rejected(self):
        page = '<h1>One Piece</h1><p>News</p><a href="https://example.org/one-piece-season-3">One Piece</a>'
        report = self.discover({self.index: page, "https://example.org/one-piece-season-3": "<h1>One Piece</h1>"})
        self.assertEqual(report["candidateUrls"], [self.index])

    def test_similar_title_rejected(self):
        report = self.discover({self.index: "<h1>The One Piece</h1><p>News</p>"})
        self.assertEqual(report["result"], "insufficient_evidence")

    def test_spinoff_page_rejected(self):
        report = self.discover({self.index: "<h1>One Piece: New Adventures</h1><p>News</p>"})
        self.assertFalse(report["candidateUrls"])

    def test_generic_provider_homepage_rejected(self):
        report = self.discover({self.index: "<h1>Netflix</h1><p>News</p>"})
        self.assertFalse(report["candidateUrls"])

    def test_redirect_outside_official_domain_rejected(self):
        report = self.discover({self.index: ("<h1>One Piece</h1><p>News</p>", "https://example.org/one-piece")})
        self.assertFalse(report["candidateUrls"])
        self.assertIn("redirect", report["warnings"][0])

    def test_http_failure_reported_without_fake_candidate(self):
        report = self.discover({self.index: u.ProviderUnavailable("HTTP 503")})
        self.assertEqual(report["result"], "provider_unavailable")
        self.assertIn("HTTP 503", report["warnings"][0])
        self.assertEqual(report["candidateUrls"], [])
        self.assertEqual(report["attemptedOfficialUrls"], [self.index])

    def test_wbd_blocked_urls_are_attempts_not_validated_candidates(self):
        meta = {"tmdbId": 100088, "title": "The Last of Us", "networks": [{"name": "HBO"}]}
        report = self.discover({}, meta)
        self.assertEqual(report["provider"], "WBD")
        self.assertEqual(report["candidateUrls"], [])
        self.assertTrue(report["attemptedOfficialUrls"])

    def test_malformed_url_rejected(self):
        self.assertFalse(u.allowed("not-a-url", self.specs["NETFLIX"]["candidateDomains"]))
        self.assertFalse(u.allowed("http://www.netflix.com/tudum/one-piece", self.specs["NETFLIX"]["candidateDomains"]))

    def test_duplicate_article_candidates_deduplicated(self):
        article = "https://www.netflix.com/tudum/articles/one-piece-season-three"
        page = f'<h1>One Piece</h1><p>News</p><a href="{article}">One Piece</a><a href="{article}">Again</a>'
        report = self.discover({self.index: page, article: "<h1>One Piece Season Three</h1>"})
        self.assertEqual(report["candidateUrls"].count(article), 1)

    def test_wrong_show_article_rejected(self):
        article = "https://www.netflix.com/tudum/articles/one-piece-season-three"
        page = f'<h1>One Piece</h1><p>News</p><a href="{article}">News</a>'
        report = self.discover({self.index: page, article: "<h1>THE ONE PIECE Anime Season 3</h1>"})
        self.assertNotIn(article, report["candidateUrls"])

    def test_wrong_season_or_spinoff_article_rejected(self):
        article = "https://www.netflix.com/tudum/articles/one-piece-season-three"
        page = f'<h1>One Piece</h1><p>News</p><a href="{article}">News</a>'
        report = self.discover({self.index: page, article: "<h1>One Piece Spinoff Season 3</h1>"})
        self.assertNotIn(article, report["candidateUrls"])

    def test_article_season_claim_never_becomes_a_verified_fact(self):
        article = "https://www.netflix.com/tudum/articles/one-piece-season-nine"
        page = f'<h1>One Piece</h1><p>News</p><a href="{article}">News</a>'
        report = self.discover({self.index: page, article: "<h1>One Piece Season Nine News</h1>"})
        self.assertIn(article, report["candidateUrls"])
        candidate = next(c for c in report["candidates"] if c["candidateUrl"] == article)
        self.assertEqual(candidate["evidenceLevel"], "SUPPORTED")
        self.assertFalse(candidate["factualParserEligible"])
        self.assertNotIn("nextSeasonNumber", report)
        self.assertNotIn("status", report)

    def test_trusted_registry_takes_precedence(self):
        meta = {"tmdbId": 153312, "title": "Tulsa King", "networks": [{"name": "Netflix"}]}
        report = d.discover(meta, self.registry, lambda *_: self.fail("should not fetch"), self.specs)
        self.assertEqual((report["result"], report["provider"]), ("trusted_registry", "PARAMOUNT"))

    def test_amazon_index_link_supported_not_parser_eligible(self):
        meta = {"tmdbId": 213306, "title": "Cross", "networks": [{"name": "Prime Video"}]}
        index = "https://www.aboutamazon.com/entertainment-news"
        article = "https://www.aboutamazon.com/news/entertainment/cross-season-3-prime-video"
        report = self.discover({index: f'<h1>Entertainment News</h1><a href="{article}">Cross</a>',
                                article: "<h1>Cross Season 3 News</h1>"}, meta)
        self.assertEqual(report["candidateUrls"], [article])
        self.assertEqual(report["evidenceLevel"], "SUPPORTED")
        self.assertFalse(report["candidates"][0]["factualParserEligible"])

    def test_minimal_metadata_works_without_provider(self):
        report = self.discover({}, {"tmdbId": 123456, "title": "Example Series"})
        self.assertEqual(report["result"], "insufficient_evidence")

    def test_invalid_metadata_rejected(self):
        with self.assertRaisesRegex(ValueError, "positive tmdbId"):
            self.discover({}, {"tmdbId": -1, "title": "Example"})

    def test_tmdb_details_are_routing_only(self):
        def fetch(url, domains, headers):
            self.assertEqual(domains, ["api.themoviedb.org"])
            self.assertEqual(headers["Authorization"], "Bearer local-test-token")
            return json.dumps({"id": 111110, "name": "One Piece", "original_name": "ONE PIECE", "networks": [{"name": "Netflix"}],
                               "status": "Canceled", "next_episode_to_air": {"air_date": "2027-01-01"}}), url
        meta = d.tmdb_metadata(111110, "local-test-token", fetch)
        self.assertNotIn("status", meta)
        self.assertNotIn("next_episode_to_air", meta)

    def test_discovery_does_not_mutate_production_files(self):
        paths = [u.DATA, u.REGISTRY, u.AUDIT]
        before = [path.read_bytes() if path.exists() else None for path in paths]
        self.discover({self.index: "<h1>One Piece</h1><p>News</p>"})
        self.assertEqual(before, [path.read_bytes() if path.exists() else None for path in paths])


if __name__ == "__main__":
    unittest.main()
