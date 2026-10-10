from __future__ import annotations

import copy
import datetime as dt
from email.message import Message
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

import provider_audit as pa


HTML_INDEX = ('<html><title>Press News</title><h1>Official News</h1>'
              '<a href="/news/test-renewed-season-two">Series announcement</a></html>')
HTML_ARTICLE = ('<html><head><meta property="article:published_time" content="2025-01-02T00:00:00Z"></head>'
                '<title>Test Series Renewed for Season 2</title><h1>Test Series Renewed for Season 2</h1>'
                '<p>Test Series has been renewed for Season 2.</p></html>')


def fetched(url, raw, *, status=200, content_type="text/html"):
    return ({"status": "fetched", "httpStatus": status, "finalStatus": status,
             "finalUrl": url, "redirectCount": 0, "redirectsWithinBoundary": True,
             "contentType": content_type, "responseBytes": len(raw.encode()),
             "failureCategory": None}, raw)


def fixture_provider():
    return {"provider": "TEST", "officialDomains": ["press.example.org"], "tests": [{
        "series": "Test Series", "aliases": ["Test Series"], "tmdbId": 1,
        "discoveryUrl": "https://press.example.org/news",
        "articleUrl": "https://press.example.org/news/test-renewed-season-two"}], "notes": []}


def fake_success(url, domains):
    return fetched(url, HTML_INDEX if url.endswith("/news") else HTML_ARTICLE)


class FakeResponse:
    status = 200

    @property
    def headers(self):
        headers = Message()
        headers["Content-Type"] = self.content_type
        return headers

    def __init__(self, payload, final_url, content_type="text/html; charset=utf-8"):
        self.payload = payload
        self.final_url = final_url
        self.content_type = content_type

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.final_url

    def read(self, length):
        return self.payload[:length]


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error

    def open(self, request, timeout):
        if self.error:
            raise self.error
        return self.response


class ProviderAuditTests(unittest.TestCase):
    def test_registry_config_validation_and_official_domain_boundary(self):
        registry = json.loads(pa.REGISTRY.read_text(encoding="utf-8"))
        pa.validate_registry(registry)
        case = copy.deepcopy(fixture_provider())
        case["tests"][0]["articleUrl"] = "https://press.example.org.evil.test/news"
        with self.assertRaises(ValueError):
            pa.validate_registry({"schemaVersion": 1, "providers": [case]})
        self.assertTrue(pa.update.allowed("https://press.example.org/path", ["press.example.org"]))
        self.assertFalse(pa.update.allowed("https://press.example.org.evil.test/path", ["press.example.org"]))
        self.assertFalse(pa.update.allowed("http://press.example.org/path", ["press.example.org"]))

    def test_redirect_leaving_ownership_boundary_is_rejected(self):
        handler = pa.BoundedRedirect(["press.example.org"])
        req = Request("https://press.example.org/news")
        with self.assertRaises(pa.RedirectRejected):
            handler.redirect_request(req, None, 302, "Found", {}, "https://lookalike.test/redirect")
        self.assertEqual(pa.classify_failure(pa.RedirectRejected())[0], "REDIRECT_REJECTED")

    def test_http_403_429_timeout_and_dns_are_safe_categories(self):
        for code in (403, 429):
            error = HTTPError("https://press.example.org", code, "blocked", {}, None)
            expected = f"HTTP_{code}"
            self.assertEqual(pa.classify_failure(error), (expected, code))
        self.assertEqual(pa.classify_failure(TimeoutError("secret details"))[0], "TIMEOUT")
        self.assertEqual(pa.classify_failure(URLError(socket.gaierror("private host")))[0], "DNS_FAILURE")

    def test_report_urls_strip_query_and_fragment_values(self):
        self.assertEqual(pa._public_url("https://press.example.org/article?token=secret#section"),
                         "https://press.example.org/article")
        self.assertNotEqual(pa._canonical_url("https://press.example.org/article?id=1"),
                            pa._canonical_url("https://press.example.org/article?id=2"))

    def test_oversize_and_unsupported_content_are_rejected(self):
        too_large = b"x" * (pa.MAX_BYTES + 1)
        with patch.object(pa, "build_opener", return_value=FakeOpener(FakeResponse(too_large, "https://press.example.org/a"))):
            report, raw = pa.fetch_url("https://press.example.org/a", ["press.example.org"])
        self.assertIsNone(raw)
        self.assertEqual(report["failureCategory"], "RESPONSE_TOO_LARGE")
        with patch.object(pa, "build_opener", return_value=FakeOpener(FakeResponse(b"%PDF-", "https://press.example.org/a", "application/pdf"))):
            report, raw = pa.fetch_url("https://press.example.org/a", ["press.example.org"])
        self.assertIsNone(raw)
        self.assertEqual(report["failureCategory"], "UNSUPPORTED_CONTENT")

    def test_article_extracts_title_date_and_existing_parser_evidence(self):
        content = pa._content(HTML_ARTICLE, "https://press.example.org/news/test-renewed-season-two", fixture_provider()["tests"][0])
        self.assertTrue(content["extractable"])
        self.assertEqual(content["publicationDate"], "2025-01-02")
        self.assertIn("renewal", content["signals"])
        self.assertEqual(content["seasonNumbers"], [2])
        self.assertTrue(content["factualParserProducedFactForFixture"])

    def test_fetchable_does_not_imply_discoverable_or_parser_eligible(self):
        provider = fixture_provider()
        result = pa.audit_provider(provider, lambda url, domains: fetched(url, "<html><title>Generic</title><h1>Generic</h1></html>"))
        self.assertTrue(result["fetchable"])
        self.assertFalse(result["discoveryViable"])
        self.assertFalse(result["factualParserProducedFactForFixture"])
        self.assertNotEqual(result["recommendation"], "READY_FOR_IMPLEMENTATION")

    def test_discoverable_does_not_imply_parser_eligible(self):
        provider = fixture_provider()
        def weak(url, domains):
            return fetched(url, HTML_INDEX if url.endswith("/news") else
                           "<html><title>Test Series Season 2 Details</title><h1>Test Series Season 2 Details</h1><p>Test Series discusses season 2.</p></html>")
        result = pa.audit_provider(provider, weak)
        self.assertTrue(result["articleDiscovered"])
        self.assertFalse(result["discoveryViable"])
        self.assertFalse(result["factualParserProducedFactForFixture"])
        self.assertNotEqual(result["recommendation"], "READY_FOR_IMPLEMENTATION")

    def test_weak_evidence_never_becomes_parser_fact(self):
        weak = HTML_ARTICLE.replace("Renewed for Season 2", "Season 2 Details").replace("has been renewed for Season 2", "may return for Season 2")
        result = pa._content(weak, "https://press.example.org/a", fixture_provider()["tests"][0])
        self.assertFalse(result["factualParserProducedFactForFixture"])

    def test_blocked_provider_cannot_be_ready(self):
        provider = fixture_provider()
        def blocked(url, domains):
            return ({"status": "blocked", "failureCategory": "HTTP_403", "finalUrl": None}, None)
        result = pa.audit_provider(provider, blocked, github_actions=True)
        self.assertEqual(result["recommendation"], "BLOCKED_FROM_GITHUB_RUNNER")

    def test_http_200_challenge_page_is_classified_as_runner_block(self):
        provider = fixture_provider()
        challenge = "<html><title>Access Denied</title><h1>Access Denied</h1><p>Request blocked by security policy</p></html>"
        def challenged(url, domains):
            return fetched(url, HTML_INDEX if url.endswith("/news") else challenge)
        result = pa.audit_provider(provider, challenged, github_actions=True)
        self.assertEqual(result["tests"][0]["content"]["blockClassification"], "WAF_BLOCKED")
        self.assertEqual(result["recommendation"], "BLOCKED_FROM_GITHUB_RUNNER")

    def test_link_and_parser_evidence_must_belong_to_same_probe_case(self):
        provider = fixture_provider()
        first = copy.deepcopy(provider["tests"][0])
        second = copy.deepcopy(provider["tests"][0])
        first["discoveryUrl"], first["articleUrl"] = "https://press.example.org/case1-index", "https://press.example.org/case1-article"
        second["discoveryUrl"], second["articleUrl"] = "https://press.example.org/case2-index", "https://press.example.org/case2-article"
        provider["tests"] = [first, second]
        def fetch_case(url, domains):
            if url.endswith("case1-index"):
                return fetched(url, "<html><title>Index</title><h1>Index</h1></html>")
            if url.endswith("case2-index"):
                return fetched(url, "<html><title>Index</title><h1>Index</h1><a href='/case2-article'>Test Series</a></html>")
            if url.endswith("case1-article"):
                return fetched(url, HTML_ARTICLE)
            return fetched(url, "<html><title>Test Series Season 2 Details</title><h1>Test Series Season 2 Details</h1><p>Test Series discusses season 2.</p></html>")
        result = pa.audit_provider(provider, fetch_case, github_actions=False,
                                   today=dt.date(2025, 1, 2))
        self.assertTrue(result["articleDiscovered"])
        self.assertTrue(result["factualParserProducedFactForFixture"])
        self.assertFalse(result["discoveryViable"])

    def test_local_access_block_is_not_reported_as_github_runner_result(self):
        provider = fixture_provider()
        def blocked(url, domains):
            return ({"status": "blocked", "failureCategory": "HTTP_403", "finalUrl": None}, None)
        result = pa.audit_provider(provider, blocked, github_actions=False)
        self.assertEqual(result["recommendation"], "NEEDS_MORE_RESEARCH")

    def test_order_is_stable_and_report_does_not_dump_article_body(self):
        one, two = fixture_provider(), fixture_provider()
        one["provider"] = "Z_TEST"
        two["provider"] = "A_TEST"
        report1 = pa.audit({"schemaVersion": 1, "providers": [one, two]}, fetcher=fake_success, github_actions=False)
        report2 = pa.audit({"schemaVersion": 1, "providers": [two, one]}, fetcher=fake_success, github_actions=False)
        self.assertEqual(json.dumps(report1, sort_keys=True), json.dumps(report2, sort_keys=True))
        encoded = json.dumps(report1)
        self.assertNotIn("Test Series has been renewed", encoded)
        self.assertNotIn("articleBody", encoded)

    def test_read_only_audit_does_not_touch_protected_data(self):
        snapshots = {path: path.read_bytes() for path in pa.PROTECTED_PATHS if path.exists()}
        pa.audit_provider(fixture_provider(), fake_success)
        self.assertEqual(snapshots, {path: path.read_bytes() for path in snapshots})

    def test_output_path_rejects_protected_files(self):
        with self.assertRaises(ValueError):
            pa._safe_output_path(str(next(iter(pa.PROTECTED_PATHS))))

    def test_only_qualified_audit_providers_enter_production_discovery_allowlist(self):
        audit_registry = json.loads(pa.REGISTRY.read_text(encoding="utf-8"))
        discovery_registry = json.loads((pa.ROOT / "official-data" / "discovery_providers.json").read_text(encoding="utf-8"))
        production_domains = {domain for spec in discovery_registry.values() for domain in spec["officialDomains"]}
        audit_domains = {domain for item in audit_registry["providers"] for domain in item["officialDomains"]}
        self.assertTrue(audit_domains - production_domains)
        self.assertIn("AMC", discovery_registry)
        self.assertIn("DISNEY_PLUS", discovery_registry)
        self.assertIn("press.disneyplus.com", production_domains)
        self.assertNotIn("FX", discovery_registry)
        self.assertNotIn("HULU", discovery_registry)
        self.assertNotIn("fxnetworks.com", production_domains)
        self.assertNotIn("press.hulu.com", production_domains)


if __name__ == "__main__":
    unittest.main()
