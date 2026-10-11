from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlencode, urlparse

import provider_audit as pa


def fetched(url, raw):
    return ({"status": "fetched", "httpStatus": 200, "initialHttpStatus": 200,
             "finalStatus": 200, "finalUrl": url, "redirectCount": 0,
             "redirectsWithinBoundary": True, "contentType": "text/html",
             "responseBytes": len(raw.encode()), "failureCategory": None}, raw)


def hulu_case():
    registry = json.loads((pa.ROOT / "official-data" / "provider_audit_registry.json").read_text(encoding="utf-8"))
    return next(case for provider in registry["providers"] if provider["provider"] == "HULU"
                for case in provider["tests"] if case["caseId"] == "hulu-handmaids-tale-s3")


def generic_renewal_article():
    return ("<html><title>Hulu Surges Past 20 Million U.S. Subscribers</title>"
            "<h1>Hulu Surges Past 20 Million U.S. Subscribers</h1><main>"
            "<h2>The Handmaid’s Tale Renewed for Season Three</h2>"
            "<p>Hulu has renewed The Handmaid’s Tale for a third season.</p>"
            "</main></html>")


class HuluDiscoveryAuditTests(unittest.TestCase):
    def test_hulu_search_keeps_apostrophe_query_and_discovers_generic_release_link(self):
        case = hulu_case()
        title_query_url = pa.HULU_SEARCH_HOME + "?s=" + urlencode({"s": case["series"]}).split("=", 1)[1]
        focused_query = pa._hulu_context_search_query(case)
        focused_query_url = pa.HULU_SEARCH_HOME + "?s=" + urlencode({"s": focused_query}).split("=", 1)[1]
        calls = []

        def fetch(url, domains):
            calls.append(url)
            if url == case["discoveryRoots"][0]:
                return fetched(url, "<html><title>News</title><h1>News</h1></html>")
            if url == pa.HULU_SEARCH_HOME:
                return fetched(url, '<form action="/" method="get"><input name="s"></form>')
            if url == title_query_url:
                return fetched(url, '<a href="https://press.hulu.com/pressrelease/unrelated/">'
                               "The Handmaid's Tale history</a>")
            if url == focused_query_url:
                # Hulu search finds body matches even when the release headline
                # and URL use a generic company announcement title.
                return fetched(url, f'<a href="{case["articleUrl"]}">Hulu Surges Past 20 Million Subscribers</a>')
            if url == "https://press.hulu.com/pressrelease/unrelated/":
                return fetched(url, "<html><title>Unrelated</title><h1>Unrelated</h1></html>")
            if url == case["articleUrl"]:
                return fetched(url, generic_renewal_article())
            raise AssertionError(f"Unexpected Hulu URL: {url}")

        report = pa.qualify_provider({"provider": "HULU", "officialDomains": ["press.hulu.com"],
                                      "tests": [case]}, fetch, github_actions=False,
                                     today=dt.date(2026, 1, 1))
        result = report["tests"][0]
        self.assertIn("%27", title_query_url)
        self.assertIn("renewed+season+three", focused_query_url)
        self.assertIn(title_query_url, calls)
        self.assertIn(focused_query_url, calls)
        self.assertIn(case["articleUrl"], calls)
        self.assertTrue(result["discoveredFromRoot"])
        self.assertEqual(result["discoveryProvenance"], "hulu_official_site_search_context")
        search_attempts = [row for row in result["http"] if row.get("query")]
        self.assertEqual([row["query"] for row in search_attempts],
                         [case["series"], focused_query])
        self.assertEqual([row["source"] for row in search_attempts],
                         ["hulu_official_site_search", "hulu_official_site_search_context"])
        self.assertTrue(result["passed"])

    def test_later_hulu_case_reserves_context_search_and_candidate_budget(self):
        case = hulu_case()
        title_query_url = pa.HULU_SEARCH_HOME + "?s=" + urlencode({"s": case["series"]}).split("=", 1)[1]
        focused_query = pa._hulu_context_search_query(case)
        focused_query_url = pa.HULU_SEARCH_HOME + "?s=" + urlencode({"s": focused_query}).split("=", 1)[1]
        filler_links = "".join(
            f'<a href="https://press.hulu.com/pressrelease/filler-{index}/">'
            f"The Handmaid's Tale renewal result</a>"
            for index in range(pa.MAX_OFFICIAL_SEARCH_RESULTS))
        calls = []

        def fetch(url, domains):
            calls.append(url)
            if url in (case["discoveryRoots"][0], pa.HULU_SEARCH_HOME):
                return fetched(url, '<form action="/" method="get"><input name="s"></form>')
            if url == title_query_url:
                return fetched(url, filler_links)
            if url == focused_query_url:
                return fetched(url, f'<a href="{case["articleUrl"]}">Hulu corporate announcement</a>')
            if url == case["articleUrl"]:
                return fetched(url, generic_renewal_article())
            if "/pressrelease/filler-" in url:
                return fetched(url, "<html><title>Filler</title><h1>Filler</h1></html>")
            raise AssertionError(f"Unexpected Hulu URL: {url}")

        budget = {"remaining": 7, "reserve": 0}
        fetches, found = pa._official_search_candidates(case, ["press.hulu.com"], fetch, budget, "HULU")
        search_rows = [row for row in fetches if row.get("query")]
        self.assertEqual([row["query"] for row in search_rows], [case["series"], focused_query])
        self.assertEqual(found[0]["source"], "hulu_official_site_search_context")
        self.assertEqual(len(calls), 7)
        self.assertEqual(budget["remaining"], 0)

    def test_provider_budget_keeps_handmaid_context_query_reachable(self):
        registry = json.loads((pa.ROOT / "official-data" / "provider_audit_registry.json").read_text(encoding="utf-8"))
        provider = next(item for item in registry["providers"] if item["provider"] == "HULU")
        handmaid = next(case for case in provider["tests"] if case["caseId"] == "hulu-handmaids-tale-s3")
        filler_links = "".join(
            f'<a href="https://press.hulu.com/pressrelease/budget-filler-{index}/">'
            "Hulu announcement</a>"
            for index in range(pa.MAX_OFFICIAL_SEARCH_RESULTS))
        attempted = []

        def fetch(url, domains):
            attempted.append(url)
            if url == pa.HULU_SEARCH_HOME:
                return fetched(url, '<form action="/" method="get"><input name="s"></form>')
            if "/shows/" in url or url.endswith("/news/"):
                return fetched(url, "<html><title>News</title><h1>News</h1></html>")
            query = parse_qs(urlparse(url).query).get("s", [None])[0]
            if query:
                if query == pa._hulu_context_search_query(handmaid):
                    return fetched(url, f'<a href="{handmaid["articleUrl"]}">Hulu corporate announcement</a>')
                return fetched(url, filler_links)
            if "/pressrelease/budget-filler-" in url:
                return fetched(url, "<html><title>Hulu announcement</title><h1>Hulu announcement</h1></html>")
            if url == handmaid["articleUrl"]:
                return fetched(url, generic_renewal_article())
            return fetched(url, "<html><title>Hulu announcement</title><h1>Hulu announcement</h1></html>")

        report = pa.qualify_provider(provider, fetch, github_actions=False, today=dt.date(2026, 1, 1))
        result = next(row for row in report["tests"] if row["caseId"] == handmaid["caseId"])
        attempted_queries = [row.get("query") for row in result["http"] if row.get("query")]
        self.assertIn(handmaid["series"], attempted_queries)
        self.assertIn(pa._hulu_context_search_query(handmaid), attempted_queries)
        self.assertTrue(result["discoveredFromRoot"])
        self.assertEqual(result["discoveryProvenance"], "hulu_official_site_search_context")
        self.assertLessEqual(len(attempted), pa.MAX_PROVIDER_QUALIFICATION_FETCHES)

    def test_link_dense_early_hulu_case_preserves_budget_for_handmaid(self):
        registry = json.loads((pa.ROOT / "official-data" / "provider_audit_registry.json").read_text(encoding="utf-8"))
        provider = next(item for item in registry["providers"] if item["provider"] == "HULU")
        cases = provider["tests"]
        handmaid = next(case for case in cases if case["caseId"] == "hulu-handmaids-tale-s3")
        dense_case = cases[0]
        dense_root_html = "".join(
            f'<a href="https://press.hulu.com/pressrelease/root-filler-{index}/">'
            f"{dense_case['series']} archive item</a>"
            for index in range(pa.MAX_QUALIFICATION_LINKS_PER_PAGE))
        search_html = "".join(
            f'<a href="https://press.hulu.com/pressrelease/search-filler-{index}/">'
            "Hulu announcement</a>"
            for index in range(pa.MAX_OFFICIAL_SEARCH_RESULTS))
        attempted = []

        def fetch(url, domains):
            attempted.append(url)
            if url == pa.HULU_SEARCH_HOME:
                return fetched(url, '<form action="/" method="get"><input name="s"></form>')
            if url == dense_case["discoveryRoots"][0]:
                return fetched(url, dense_root_html)
            if "/shows/" in url or url.endswith("/news/"):
                return fetched(url, "<html><title>News</title><h1>News</h1></html>")
            query = parse_qs(urlparse(url).query).get("s", [None])[0]
            if query:
                if query == pa._hulu_context_search_query(handmaid):
                    return fetched(url, f'<a href="{handmaid["articleUrl"]}">Hulu corporate announcement</a>')
                query_results = search_html.replace("Hulu announcement", f"{query} announcement")
                return fetched(url, query_results)
            if "/pressrelease/root-filler-" in url or "/pressrelease/search-filler-" in url:
                return fetched(url, "<html><title>Hulu announcement</title><h1>Hulu announcement</h1></html>")
            if url == handmaid["articleUrl"]:
                return fetched(url, generic_renewal_article())
            return fetched(url, "<html><title>Hulu announcement</title><h1>Hulu announcement</h1></html>")

        provider = {**provider, "tests": [cases[0], cases[1], handmaid]}
        report = pa.qualify_provider(provider, fetch, github_actions=False, today=dt.date(2026, 1, 1))
        result = next(row for row in report["tests"] if row["caseId"] == handmaid["caseId"])
        query_values = [row.get("query") for row in result["http"] if row.get("query")]
        self.assertIn(handmaid["series"], query_values)
        self.assertIn(pa._hulu_context_search_query(handmaid), query_values)
        self.assertEqual(result["discoveryProvenance"], "hulu_official_site_search_context")
        self.assertTrue(result["passed"])
        self.assertEqual(len(attempted), pa.MAX_PROVIDER_QUALIFICATION_FETCHES)

    def test_hulu_search_result_traversal_is_bounded(self):
        case = hulu_case()
        case["expected"] = {}
        query_url = pa.HULU_SEARCH_HOME + "?s=" + urlencode({"s": case["series"]}).split("=", 1)[1]
        result_links = [f"https://press.hulu.com/pressrelease/unrelated-{index}/"
                        for index in range(pa.MAX_OFFICIAL_SEARCH_RESULTS + 5)]
        search_html = "".join(f'<a href="{url}">The Handmaid\'s Tale announcement</a>' for url in result_links)
        calls = []

        def fetch(url, domains):
            calls.append(url)
            if url == pa.HULU_SEARCH_HOME:
                return fetched(url, '<form action="/" method="get"><input name="s"></form>')
            if url == query_url:
                return fetched(url, search_html)
            return fetched(url, "<html><title>Unrelated</title><h1>Unrelated</h1></html>")

        budget = {"remaining": pa.MAX_PROVIDER_QUALIFICATION_FETCHES}
        fetches, found = pa._official_search_candidates(case, ["press.hulu.com"], fetch, budget, "HULU")
        article_calls = [url for url in calls if "/pressrelease/" in url]
        self.assertEqual(len(article_calls), pa.MAX_OFFICIAL_SEARCH_RESULTS, calls)
        self.assertNotIn(case["articleUrl"], article_calls)
        self.assertFalse(found)
        self.assertEqual(len(fetches), pa.MAX_OFFICIAL_SEARCH_RESULTS + 2)

    def test_fetchable_fixture_url_alone_is_diagnostic_not_discovery(self):
        case = hulu_case()
        title_query_url = pa.HULU_SEARCH_HOME + "?s=" + urlencode({"s": case["series"]}).split("=", 1)[1]
        focused_query_url = pa.HULU_SEARCH_HOME + "?s=" + urlencode({
            "s": pa._hulu_context_search_query(case)}).split("=", 1)[1]

        def fetch(url, domains):
            if url == case["discoveryRoots"][0]:
                return fetched(url, "<html><title>News</title><h1>News</h1></html>")
            if url == pa.HULU_SEARCH_HOME:
                return fetched(url, '<form action="/" method="get"><input name="s"></form>')
            if url in (title_query_url, focused_query_url):
                return fetched(url, '<a href="https://press.hulu.com/pressrelease/unrelated/">Unrelated announcement</a>')
            if url == "https://press.hulu.com/pressrelease/unrelated/":
                return fetched(url, "<html><title>Unrelated</title><h1>Unrelated</h1></html>")
            if url == case["articleUrl"]:
                return fetched(url, generic_renewal_article())
            raise AssertionError(f"Unexpected Hulu URL: {url}")

        report = pa.qualify_provider({"provider": "HULU", "officialDomains": ["press.hulu.com"],
                                      "tests": [case]}, fetch, github_actions=False,
                                     today=dt.date(2026, 1, 1))
        result = report["tests"][0]
        self.assertTrue(result["knownArticleDiagnostic"]["fetchable"])
        self.assertEqual(result["knownArticleDiagnostic"]["source"], "diagnostic_only")
        self.assertFalse(result["discoveredFromRoot"])
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
