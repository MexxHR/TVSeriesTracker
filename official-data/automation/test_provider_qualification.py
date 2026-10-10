from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import unittest

import provider_audit as pa


FIXTURES = Path(__file__).parent / "fixtures"
HULU_ROOT = "https://press.example.org/shows/deli-boys/press-releases/"
HULU_ARTICLE = "https://press.example.org/pressrelease/hulus-critically-acclaimed-comedy-series-deli-boys-renewed-for-a-second-season/"


def fetched(url, raw, **extra):
    return ({"status": "fetched", "httpStatus": 200, "initialHttpStatus": 200,
             "finalStatus": 200, "finalUrl": url, "redirectCount": 0,
             "redirectsWithinBoundary": True, "contentType": "text/html",
             "responseBytes": len(raw.encode()), "failureCategory": None, **extra}, raw)


def provider(cases):
    return {"provider": "TEST", "officialDomains": ["press.example.org"],
            "tests": cases, "notes": []}


def case(case_id, article, *, series="Test Series", kind="positive", root=None,
         expected=None):
    return {"caseId": case_id, "kind": kind, "series": series,
            "aliases": [series], "articleUrl": article,
            "discoveryRoots": [root or f"https://press.example.org/index/{case_id}"],
            "expected": expected or {"factTypes": ["LIFECYCLE"], "status": "RENEWED", "season": 2}}


def article(title="Test Series renewed for season 2", body=None):
    body = body or "Test Series has been renewed for season 2."
    return f"<html><title>{title}</title><h1>{title}</h1><main><p>{body}</p></main></html>"


def series_article(series):
    return article(f"{series} renewed for season 2", f"{series} has been renewed for season 2.")


def amc_provider(item):
    return {"provider": "AMC", "officialDomains": ["amcglobalmedia.com"],
            "tests": [item], "notes": []}


def amc_daryl_case():
    return {"caseId": "amc-daryl-final-s4", "kind": "positive",
            "series": "The Walking Dead: Daryl Dixon",
            "aliases": ["The Walking Dead: Daryl Dixon", "Daryl Dixon"],
            "articleUrl": "https://www.amcglobalmedia.com/2025/07/28/the-walking-dead-daryl-dixon-renewed-for-a-fourth-and-final-season/",
            "discoveryRoots": ["https://www.amcglobalmedia.com/press-/"],
            "expected": {"titleMarkers": ["Daryl Dixon"], "factTypes": ["LIFECYCLE"],
                         "status": "FINAL_SEASON", "season": 4}}


class ProviderQualificationTests(unittest.TestCase):
    def test_multi_case_requires_three_independent_positive_articles(self):
        series_names = ["Series One", "Series Two", "Series Three"]
        cases = [case(f"c{i}", f"https://press.example.org/article/{i}", series=series_names[i - 1])
                 for i in range(1, 4)]

        def fetch(url, domains):
            if "/index/" in url:
                target = url.rsplit("/", 1)[-1]
                number = target[1:]
                return fetched(url, f"<html><title>Official index</title><h1>Official index</h1><a href='/article/{number}'>Announcement</a></html>")
            return fetched(url, series_article(series_names[int(url.rsplit("/", 1)[-1]) - 1]))

        result = pa.qualify_provider(provider(cases), fetch, github_actions=False, today=dt.date(2026, 1, 1))
        self.assertEqual(result["passedCaseCount"], 3)
        self.assertTrue(result["qualificationCandidate"])
        self.assertFalse(result["qualificationReady"])
        self.assertEqual(result["recommendation"], "LOCAL_QUALIFICATION_CANDIDATE")

    def test_three_articles_about_same_series_are_not_independent(self):
        cases = [case(f"same{i}", f"https://press.example.org/article/{i}") for i in range(1, 4)]

        def fetch(url, domains):
            if "/index/" in url:
                number = url.rsplit("/", 1)[-1][-1]
                return fetched(url, f"<html><title>Index</title><h1>Index</h1><a href='/article/{number}'>Announcement</a></html>")
            return fetched(url, series_article("Test Series"))

        result = pa.qualify_provider(provider(cases), fetch, github_actions=False, today=dt.date(2026, 1, 1))
        self.assertEqual(result["passedCaseCount"], 3)
        self.assertFalse(result["multiCaseDiscoveryPassed"])
        self.assertFalse(result["qualificationCandidate"])

    def test_one_case_never_qualifies_even_when_discovered_and_parser_compatible(self):
        one = case("only", "https://press.example.org/article/only")
        result = pa.qualify_provider(provider([one]), lambda url, domains: fetched(
            url, "<html><title>Index</title><h1>Index</h1><a href='/article/only'>Test Series</a></html>"
            if "/index/" in url else article()), github_actions=True, today=dt.date(2026, 1, 1))
        self.assertEqual(result["passedCaseCount"], 1)
        self.assertFalse(result["qualificationCandidate"])
        self.assertFalse(result["qualificationReady"])

    def test_known_article_url_is_never_fetched_as_a_discovery_seed(self):
        item = case("known-only", "https://press.example.org/article/known-only")
        calls = []

        def fetch(url, domains):
            calls.append(url)
            return fetched(url, "<html><title>Index</title><h1>Index</h1></html>")

        result = pa.qualify_provider(provider([item]), fetch, github_actions=True)
        self.assertEqual(calls[-1], item["articleUrl"])  # diagnostic probe follows discovery traversal
        self.assertEqual(sum(pa._canonical_url(url) == pa._canonical_url(item["articleUrl"]) for url in calls), 1)
        self.assertFalse(result["tests"][0]["discoveredFromRoot"])
        self.assertEqual(result["tests"][0]["knownArticleDiagnostic"]["source"], "diagnostic_only")
        self.assertEqual(result["tests"][0]["failureCategory"], "KNOWN_ARTICLE_NOT_DISCOVERED")
        self.assertTrue(result["tests"][0]["knownArticleDiagnostic"]["fetchable"])
        self.assertFalse(result["tests"][0]["knownArticleDiagnostic"]["parserCompatible"])

    def test_link_from_official_index_counts_as_discovery(self):
        item = case("linked", "https://press.example.org/article/linked")

        def fetch(url, domains):
            raw = ("<html><title>Press</title><h1>Press</h1><a href='/article/linked'>Announcement</a></html>"
                   if "/index/" in url else article())
            return fetched(url, raw)

        result = pa.qualify_provider(provider([item]), fetch, github_actions=True, today=dt.date(2026, 1, 1))
        self.assertTrue(result["tests"][0]["discoveredFromRoot"])
        self.assertTrue(result["tests"][0]["parserCompatible"])

    def test_article_path_guessing_does_not_count_as_discovery(self):
        item = case("guess", "https://press.example.org/article/guess")
        result = pa.qualify_provider(provider([item]), lambda url, domains: fetched(
            url, "<html><title>Press</title><h1>Press</h1><p>No article links here.</p></html>"), github_actions=True)
        self.assertFalse(result["tests"][0]["discoveredFromRoot"])

    def test_exact_target_link_is_prioritized_over_unrelated_root_links(self):
        item = case("priority", "https://press.example.org/article/priority")
        calls = []

        def fetch(url, domains):
            calls.append(url)
            if "/index/" in url:
                noise = "".join(f"<a href='/news/unrelated-{i}'>Unrelated {i}</a>" for i in range(100))
                return fetched(url, f"<html><title>Index</title><h1>Index</h1>{noise}<a href='/article/priority'>Test Series</a></html>")
            return fetched(url, article())

        pa.qualify_provider(provider([item]), fetch, github_actions=False, today=dt.date(2026, 1, 1))
        self.assertEqual(calls[:2], [item["discoveryRoots"][0], item["articleUrl"]])

    def test_official_archive_pagination_inside_nav_can_discover_article(self):
        root = "https://press.example.org/press-/"
        target = "https://press.example.org/article/season-two"
        item = case("paged", target, root=root)

        def fetch(url, domains):
            if url == root:
                return fetched(url, "<html><title>Press archive</title><h1>Press archive</h1>"
                               "<nav><a href='/press-/page/2/'>2</a></nav></html>")
            if url.endswith("/page/2/"):
                return fetched(url, "<html><title>Page 2</title><h1>Page 2</h1>"
                               "<a href='/article/season-two'>Test Series renewal</a></html>")
            return fetched(url, article())

        result = pa.qualify_provider(provider([item]), fetch, github_actions=False,
                                     today=dt.date(2026, 1, 1))
        self.assertTrue(result["tests"][0]["discoveredFromRoot"])
        self.assertTrue(result["tests"][0]["passed"])

    def test_archive_pagination_rejects_distant_page_link(self):
        root = "https://press.example.org/press-/"
        target = "https://press.example.org/article/missing"
        item = case("bounded", target, root=root)
        calls = []

        def fetch(url, domains):
            calls.append(url)
            if url == root:
                return fetched(url, "<html><title>Archive</title><h1>Archive</h1>"
                               "<nav><a href='/press-/page/563/'>Last</a></nav></html>")
            return fetched(url, article())

        result = pa.qualify_provider(provider([item]), fetch, github_actions=False)
        self.assertFalse(result["tests"][0]["discoveredFromRoot"])
        self.assertNotIn("https://press.example.org/press-/page/563/", calls)

    def test_amc_search_form_uses_series_title_and_discovers_daryl_article(self):
        item = amc_daryl_case()
        root = item["discoveryRoots"][0]
        home = pa.AMC_SEARCH_HOME
        results = "https://www.amcglobalmedia.com/?s=The+Walking+Dead%3A+Daryl+Dixon"
        calls = []
        home_html = '<html><body><form action="https://www.amcglobalmedia.com/" method="get"><input name="s" type="search"></form></body></html>'
        article_html = article(
            "The Walking Dead: Daryl Dixon Renewed for a Fourth and Final Season",
            "The Walking Dead: Daryl Dixon has been renewed for season 4, its fourth and final season.")

        def fetch(url, domains):
            calls.append(url)
            if url == root:
                return fetched(url, "<html><title>Press archive</title><h1>Press archive</h1></html>")
            if url == home:
                return fetched(url, home_html)
            if url == results:
                return fetched(url, (FIXTURES / "amc_daryl_search_results.html").read_text(encoding="utf-8"))
            if url == item["articleUrl"]:
                return fetched(url, article_html)
            return fetched(url, "<html><title>Unrelated</title><h1>Unrelated</h1></html>")

        result = pa.qualify_provider(amc_provider(item), fetch, github_actions=False,
                                     today=dt.date(2026, 1, 1))
        case_result = result["tests"][0]
        self.assertIn(results, calls)
        self.assertTrue(case_result["discoveredFromRoot"])
        self.assertEqual(case_result["discoveryProvenance"], "amc_official_site_search")
        self.assertTrue(case_result["passed"])

    def test_amc_search_requires_same_official_host_get_form_and_never_guesses_article_url(self):
        item = amc_daryl_case()
        root = item["discoveryRoots"][0]
        calls = []
        malicious_home = ('<form action="https://search.example.net/" method="get">'
                          '<input name="s"></form>')

        def fetch(url, domains):
            calls.append(url)
            if url == root:
                return fetched(url, "<html><title>Archive</title><h1>Archive</h1></html>")
            if url == pa.AMC_SEARCH_HOME:
                return fetched(url, malicious_home)
            return fetched(url, "<html><title>Daryl Dixon</title><h1>Daryl Dixon</h1></html>")

        result = pa.qualify_provider(amc_provider(item), fetch, github_actions=False)
        self.assertFalse(result["tests"][0]["discoveredFromRoot"])
        self.assertNotIn("https://search.example.net/", calls)
        # The article URL can still be fetched once, but only as a post-traversal diagnostic.
        self.assertEqual(calls.count(item["articleUrl"]), 1)

    def test_amc_search_result_following_is_bounded(self):
        item = amc_daryl_case()
        root = item["discoveryRoots"][0]
        results_url = "https://www.amcglobalmedia.com/?s=The+Walking+Dead%3A+Daryl+Dixon"
        calls = []
        home_html = '<form action="/" method="get"><input name="s"></form>'
        candidates = "".join(
            f'<a href="/press/articles/daryl-dixon-unrelated-{n}">Daryl Dixon story {n}</a>'
            for n in range(pa.MAX_OFFICIAL_SEARCH_RESULTS + 10))

        def fetch(url, domains):
            calls.append(url)
            if url == root:
                return fetched(url, "<html><title>Archive</title><h1>Archive</h1></html>")
            if url == pa.AMC_SEARCH_HOME:
                return fetched(url, home_html)
            if url == results_url:
                return fetched(url, f"<html><h1>Results</h1>{candidates}</html>")
            return fetched(url, "<html><title>No match</title><h1>No match</h1></html>")

        pa.qualify_provider(amc_provider(item), fetch, github_actions=False)
        self.assertEqual(sum("/press/articles/" in url for url in calls), pa.MAX_OFFICIAL_SEARCH_RESULTS)
        self.assertLessEqual(len(calls), pa.MAX_OFFICIAL_SEARCH_RESULTS + 4)

    def test_hulu_first_party_search_discovers_historical_release_without_known_url_seed(self):
        item = next(row for provider_row in pa.load_registry()["providers"]
                    if provider_row["provider"] == "HULU"
                    for row in provider_row["tests"]
                    if row["caseId"] == "hulu-difficult-people-s2")
        root = item["discoveryRoots"][0]
        search_url = "https://press.hulu.com/?s=Difficult+People"
        calls = []

        def fetch(url, domains):
            calls.append(url)
            if url == root:
                return fetched(url, "<html><title>News</title><h1>News</h1></html>")
            if url == pa.HULU_SEARCH_HOME:
                return fetched(url, '<form action="/" method="get"><input name="s"></form>')
            if url == search_url:
                return fetched(url, '<html><title>Search results</title><h1>Search results</h1>'
                               f'<a href="{item["articleUrl"]}">Difficult People announcement</a></html>')
            if url == item["articleUrl"]:
                return fetched(url, article("Hulu Renews Difficult People for Season Two",
                                            "Difficult People has been renewed for season 2."))
            raise AssertionError(f"Unexpected URL: {url}")

        result = pa.qualify_provider({"provider": "HULU", "officialDomains": ["press.hulu.com"],
                                      "tests": [item]}, fetch, github_actions=False,
                                     today=dt.date(2026, 1, 1))
        self.assertIn(search_url, calls)
        self.assertEqual(result["tests"][0]["discoveryProvenance"], "hulu_official_site_search")
        self.assertTrue(result["tests"][0]["passed"])

    def test_hulu_search_without_official_link_cannot_count_known_article(self):
        item = next(row for provider_row in pa.load_registry()["providers"]
                    if provider_row["provider"] == "HULU"
                    for row in provider_row["tests"]
                    if row["caseId"] == "hulu-difficult-people-s2")
        calls = []

        def fetch(url, domains):
            calls.append(url)
            if url == pa.HULU_SEARCH_HOME:
                return fetched(url, '<form action="https://external.example/" method="get">'
                               '<input name="s"></form>')
            return fetched(url, article())

        result = pa.qualify_provider({"provider": "HULU", "officialDomains": ["press.hulu.com"],
                                      "tests": [item]}, fetch, github_actions=False)
        self.assertFalse(result["tests"][0]["discoveredFromRoot"])
        self.assertEqual(calls.count(item["articleUrl"]), 1)
        self.assertFalse(any("external.example" in url for url in calls))

    def test_fx_mayans_unbound_final_season_and_season_headings_reproduce_parser_gap(self):
        raw = (FIXTURES / "fx_mayans_unbound_final_season.html").read_text(encoding="utf-8")
        item = {"series": "Mayans M.C.", "aliases": ["Mayans M.C.", "Mayans MC"]}
        content = pa._qualification_content(raw, "https://www.fxnetworks.com/shows/mayans-mc", item,
                                             dt.date(2026, 1, 1))
        self.assertIn("final_season", content["signals"])
        self.assertEqual(content["facts"], [])
        self.assertEqual(content["parserFactCount"], 0)

    def test_fx_weak_or_unrelated_final_season_language_does_not_create_mayans_fact(self):
        samples = [
            "<html><title>Mayans M.C.</title><h1>Mayans M.C.</h1><main>"
            "<h2>Mayans M.C. Season 5</h2><p>Watch the final season now.</p></main></html>",
            "<html><title>Mayans M.C.</title><h1>Mayans M.C.</h1><main>"
            "<p>Mayans M.C. Season 5 is streaming. Snowfall's sixth and final season is also available.</p>"
            "</main></html>",
        ]
        item = {"series": "Mayans M.C.", "aliases": ["Mayans M.C.", "Mayans MC"]}
        for raw in samples:
            with self.subTest(raw=raw):
                content = pa._qualification_content(raw, "https://www.fxnetworks.com/shows/mayans-mc", item,
                                                     dt.date(2026, 1, 1))
                self.assertEqual(content["facts"], [])

    def test_fx_lowdown_and_snowfall_bound_lifecycle_statements_still_parse(self):
        controls = [
            ("The Lowdown", ["The Lowdown"], "The Lowdown renewed for season 2.", "RENEWED", 2),
            ("Snowfall", ["Snowfall"], "Snowfall's sixth and final season begins now.", "FINAL_SEASON", 6),
        ]
        for series, aliases, statement, status, season in controls:
            with self.subTest(series=series):
                raw = article(f"{series} announcement", statement)
                content = pa._qualification_content(raw, "https://www.fxnetworks.com/announcement",
                    {"series": series, "aliases": aliases}, dt.date(2026, 1, 1))
                self.assertEqual(content["facts"], [{"factType": "LIFECYCLE", "status": status,
                                                    "season": season, "releaseDate": None,
                                                    "releaseYear": None}])

    def test_external_redirect_rejected_by_fetch_result(self):
        item = case("redirect", "https://press.example.org/article/redirect")

        def fetch(url, domains):
            return ({"status": "rejected", "httpStatus": 302, "finalUrl": None,
                     "redirectCount": 1, "redirectsWithinBoundary": False,
                     "failureCategory": "REDIRECT_REJECTED"}, None)

        result = pa.qualify_provider(provider([item]), fetch, github_actions=True)
        self.assertEqual(result["tests"][0]["failureCategory"], "REDIRECT_REJECTED")
        self.assertFalse(result["tests"][0]["discoveredFromRoot"])

    def test_all_github_403_cases_are_reported_as_runner_block(self):
        item = case("blocked", "https://press.example.org/article/blocked")

        def fetch(url, domains):
            return ({"status": "blocked", "httpStatus": 403, "finalUrl": None,
                     "failureCategory": "HTTP_403", "redirectsWithinBoundary": True}, None)

        result = pa.qualify_provider(provider([item]), fetch, github_actions=True)
        self.assertEqual(result["recommendation"], "BLOCKED_FROM_GITHUB_RUNNER")

    def test_discovered_fact_mismatch_has_parser_compatibility_classification(self):
        item = case("parser-mismatch", "https://press.example.org/article/parser-mismatch")

        def fetch(url, domains):
            raw = ("<html><title>Press</title><h1>Press</h1><a href='/article/parser-mismatch'>Announcement</a></html>"
                   if "/index/" in url else article("Test Series season 2 details", "Test Series discusses season 2."))
            return fetched(url, raw)

        result = pa.qualify_provider(provider([item]), fetch, github_actions=False)
        self.assertEqual(result["recommendation"], "PARSER_COMPATIBILITY_NEEDS_WORK")

    def test_ambiguity_failure_has_safety_classification(self):
        item = case("safety", "https://press.example.org/article/safety", kind="ambiguity",
                    expected={"factTypes": ["LIFECYCLE"], "status": "RENEWED", "season": 2,
                              "forbiddenStatuses": ["FINAL_SEASON"]})

        def fetch(url, domains):
            raw = ("<html><title>Press</title><h1>Press</h1><a href='/article/safety'>Announcement</a></html>"
                   if "/index/" in url else article("Test Series renewed for season 2",
                                                    "Test Series renewed for season 2. Test Series fourth and final season."))
            return fetched(url, raw)

        result = pa.qualify_provider(provider([item]), fetch, github_actions=False, today=dt.date(2026, 1, 1))
        self.assertEqual(result["recommendation"], "AMBIGUITY_SAFETY_NEEDS_WORK")

    def test_ambiguity_cases_blocked_by_dns_are_not_safety_failures(self):
        cases = [case("dns-amb", "https://press.example.org/article/dns-amb", kind="ambiguity",
                      expected={"factTypes": ["LIFECYCLE"], "status": "RENEWED", "season": 2}),
                 case("dns-negative", "https://press.example.org/article/dns-negative", kind="negative",
                      expected={"factTypes": []})]

        def dns_failure(url, domains):
            return ({"status": "failed", "httpStatus": None, "finalUrl": None,
                     "failureCategory": "DNS_FAILURE", "redirectsWithinBoundary": None}, None)

        result = pa.qualify_provider(provider(cases), dns_failure, github_actions=False)
        self.assertEqual(result["recommendation"], "INSUFFICIENT_OFFICIAL_EVIDENCE")
        self.assertFalse(result["ambiguitySafety"])

    def test_fetchable_does_not_imply_discoverable(self):
        item = case("fetchable", "https://press.example.org/article/fetchable")
        result = pa.qualify_provider(provider([item]), lambda url, domains: fetched(
            url, "<html><title>Press</title><h1>Press</h1></html>"), github_actions=False)
        self.assertTrue(result["fetchable"])
        self.assertFalse(result["discoveryViable"])
        self.assertFalse(result["qualificationCandidate"])

    def test_discovery_does_not_imply_parser_compatibility(self):
        item = case("weak", "https://press.example.org/article/weak")

        def fetch(url, domains):
            raw = ("<html><title>Press</title><h1>Press</h1><a href='/article/weak'>Announcement</a></html>"
                   if "/index/" in url else article("Test Series Season 2 details", "Test Series discusses season 2."))
            return fetched(url, raw)

        result = pa.qualify_provider(provider([item]), fetch, github_actions=False)
        self.assertTrue(result["discoveryViable"])
        self.assertFalse(result["parserCompatibility"])
        self.assertFalse(result["qualificationCandidate"])

    def test_three_successes_with_one_failed_required_case_is_not_qualified(self):
        cases = [case(f"mixed{i}", f"https://press.example.org/article/{i}") for i in range(1, 4)]

        def fetch(url, domains):
            if "/index/" in url:
                index = url.rsplit("/", 1)[-1]
                number = index[-1]
                link = f"<a href='/article/{number}'>Announcement</a>" if number != "3" else ""
                return fetched(url, f"<html><title>Index</title><h1>Index</h1>{link}</html>")
            return fetched(url, article())

        result = pa.qualify_provider(provider(cases), fetch, github_actions=True, today=dt.date(2026, 1, 1))
        self.assertEqual(result["passedCaseCount"], 2)
        self.assertFalse(result["qualificationReady"])
        self.assertFalse(result["multiCaseDiscoveryPassed"])

    def test_negative_case_fails_if_parser_finds_fact(self):
        negative = case("negative", "https://press.example.org/article/negative", kind="negative",
                        expected={"factTypes": []})

        def fetch(url, domains):
            raw = ("<html><title>Index</title><h1>Index</h1><a href='/article/negative'>Article</a></html>"
                   if "/index/" in url else article())
            return fetched(url, raw)

        result = pa.qualify_provider(provider([negative]), fetch, github_actions=False, today=dt.date(2026, 1, 1))
        self.assertFalse(result["tests"][0]["passed"])
        self.assertEqual(result["tests"][0]["failureCategory"], "UNEXPECTED_FACT")

    def test_failed_ambiguity_case_prevents_candidate(self):
        cases = [case(f"good{i}", f"https://press.example.org/article/g{i}") for i in range(1, 4)]
        ambiguous = case("ambiguous", "https://press.example.org/article/amb", kind="ambiguity",
                         expected={"factTypes": ["LIFECYCLE"], "status": "RENEWED", "season": 2,
                                   "forbiddenStatuses": ["FINAL_SEASON"]})
        cases.append(ambiguous)

        def fetch(url, domains):
            if "/index/" in url:
                name = url.rsplit("/", 1)[-1]
                target = "amb" if name == "ambiguous" else name.replace("good", "g")
                return fetched(url, f"<html><title>Index</title><h1>Index</h1><a href='/article/{target}'>Article</a></html>")
            if url.endswith("/amb"):
                return fetched(url, article("Test Series renewed for season 2",
                                            "Test Series renewed for season 2. Test Series fourth and final season."))
            return fetched(url, article())

        result = pa.qualify_provider(provider(cases), fetch, github_actions=False, today=dt.date(2026, 1, 1))
        ambiguous_result = next(row for row in result["tests"] if row["caseId"] == "ambiguous")
        self.assertFalse(ambiguous_result["passed"])
        self.assertFalse(result["qualificationCandidate"])

    def test_local_candidate_is_not_github_qualification(self):
        series_names = ["Local One", "Local Two", "Local Three"]
        cases = [case(f"local{i}", f"https://press.example.org/article/l{i}", series=series_names[i - 1])
                 for i in range(1, 4)]

        def fetch(url, domains):
            if "/index/" in url:
                name = url.rsplit("/", 1)[-1]
                return fetched(url, f"<html><title>Index</title><h1>Index</h1><a href='/article/l{name[-1]}'>Article</a></html>")
            return fetched(url, series_article(series_names[int(url.rsplit("/", 1)[-1][-1]) - 1]))

        local = pa.qualify_provider(provider(cases), fetch, github_actions=False, today=dt.date(2026, 1, 1))
        github = pa.qualify_provider(provider(cases), fetch, github_actions=True, today=dt.date(2026, 1, 1))
        self.assertTrue(local["qualificationCandidate"])
        self.assertFalse(local["qualificationReady"])
        self.assertTrue(github["qualificationReady"])

    def test_hulu_deli_boys_final_season_signal_is_not_bound_to_deli_boys(self):
        root_raw = (FIXTURES / "hulu_deli_boys_index.html").read_text(encoding="utf-8")
        article_raw = (FIXTURES / "hulu_deli_boys_renewal_with_big_mouth_bio.html").read_text(encoding="utf-8")
        item = case("hulu-deli-boys", HULU_ARTICLE, series="Deli Boys", root=HULU_ROOT,
                    expected={"factTypes": ["LIFECYCLE"], "status": "RENEWED", "season": 2,
                              "forbiddenStatuses": ["FINAL_SEASON"], "titleMarkers": ["Deli Boys"]})
        result = pa.qualify_provider(provider([item]), lambda url, domains: fetched(
            url, root_raw if url == HULU_ROOT else article_raw), github_actions=False,
            today=dt.date(2026, 1, 1))
        test_result = result["tests"][0]
        self.assertIn("final_season", test_result["content"]["signals"])
        self.assertEqual(test_result["content"]["facts"], [{"factType": "LIFECYCLE", "status": "RENEWED",
                                                            "season": 2, "releaseDate": None,
                                                            "releaseYear": None}])
        self.assertTrue(test_result["safetyPassed"])

    def test_cross_series_signal_does_not_create_fact_for_target_series(self):
        raw = article("Test Series Renewed for Season 2",
                      "Test Series has been renewed for season 2. Big Mouth eighth and final season.")
        content = pa._qualification_content(raw, "https://press.example.org/a", case("x", "https://press.example.org/a"), dt.date(2026, 1, 1))
        self.assertTrue(content["facts"])
        self.assertEqual({fact["status"] for fact in content["facts"]}, {"RENEWED"})

    def test_cross_season_evidence_is_rejected_for_ambiguity_case(self):
        content = {"facts": [{"factType": "LIFECYCLE", "status": "RENEWED", "season": 2,
                              "releaseDate": None, "releaseYear": None},
                             {"factType": "LIFECYCLE", "status": "FINAL_SEASON", "season": 4,
                              "releaseDate": None, "releaseYear": None}]}
        ok, failure = pa._expected_facts_match(content, {"kind": "ambiguity", "expected": {
            "factTypes": ["LIFECYCLE"], "status": "RENEWED", "season": 2,
            "forbiddenStatuses": ["FINAL_SEASON"]}})
        self.assertFalse(ok)
        self.assertEqual(failure, "CROSS_SEASON_FACT")

    def test_conflicting_lifecycle_statuses_for_same_season_are_rejected(self):
        content = {"pageTitle": "Test Series", "facts": [
            {"factType": "LIFECYCLE", "status": "RENEWED", "season": 2,
             "releaseDate": None, "releaseYear": None},
            {"factType": "LIFECYCLE", "status": "FINAL_SEASON", "season": 2,
             "releaseDate": None, "releaseYear": None}]}
        ok, failure = pa._expected_facts_match(content, {"kind": "positive", "expected": {
            "factTypes": ["LIFECYCLE"], "status": "RENEWED", "season": 2}})
        self.assertFalse(ok)
        self.assertEqual(failure, "CONFLICTING_LIFECYCLE_STATUS")

    def test_series_scoped_or_series_page_cases_do_not_count_as_generic_qualification(self):
        cases = [case(f"scope{i}", f"https://press.example.org/article/scope{i}", series=f"Scope Series {i}")
                 for i in range(1, 4)]
        cases[1]["rootScope"] = "series"
        cases[2]["sourceStructure"] = "series_page"

        def fetch(url, domains):
            if "/index/" in url:
                number = url.rsplit("/", 1)[-1][-1]
                return fetched(url, f"<html><title>Index</title><h1>Index</h1><a href='/article/scope{number}'>Announcement</a></html>")
            number = url.rsplit("/", 1)[-1][-1]
            return fetched(url, series_article(f"Scope Series {number}"))

        result = pa.qualify_provider(provider(cases), fetch, github_actions=False, today=dt.date(2026, 1, 1))
        self.assertEqual(result["passedCaseCount"], 3)
        self.assertFalse(result["multiCaseDiscoveryPassed"])
        self.assertFalse(result["qualificationCandidate"])

    def test_separate_paragraphs_cannot_be_joined_into_a_fact(self):
        raw = ("<html><title>Announcement</title><h1>Announcement</h1><main>"
               "<p>Test Series shares casting news.</p><p>It has been renewed for season 2.</p></main></html>")
        content = pa._qualification_content(raw, "https://press.example.org/a", case("x", "https://press.example.org/a"), dt.date(2026, 1, 1))
        self.assertEqual(content["facts"], [])

    def test_disney_official_sitemap_discovers_three_independent_news_articles(self):
        original = next(row for row in pa.load_registry()["providers"] if row["provider"] == "DISNEY_PLUS")
        disney = copy.deepcopy(original)
        root = "https://press.disneyplus.com/sitemap.xml"
        child = "https://press.disneyplus.com/sitemap-news.xml"
        targets = [case["articleUrl"] for case in disney["tests"]]
        index = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 f'<sitemap><loc>{child}</loc></sitemap></sitemapindex>')
        urlset = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                  + "".join(f"<url><loc>{url}</loc></url>" for url in targets)
                  + '<url><loc>https://unofficial.example/news/percy-jackson</loc></url></urlset>')
        bodies = {
            targets[0]: ("Percy Jackson and the Olympians",
                         "Percy Jackson and the Olympians renewed for season 2."),
            targets[1]: ("Wizards Beyond Waverly Place",
                         "Wizards Beyond Waverly Place renewed for season 2."),
            targets[2]: ("The Proud Family: Louder and Prouder",
                         "The Proud Family: Louder and Prouder season 4 will be the final season."),
        }
        calls = []

        def fetch(url, domains):
            calls.append(url)
            if url in (root, child):
                return fetched(url, index if url == root else urlset)
            title, body = bodies[url]
            return fetched(url, article(title, body))

        result = pa.qualify_provider(disney, fetch, github_actions=False, today=dt.date(2026, 1, 1))
        self.assertEqual(result["passedCaseCount"], 3)
        self.assertTrue(result["qualificationCandidate"])
        self.assertFalse(result["qualificationReady"])
        self.assertTrue(all(row["discoveryProvenance"] == "official_sitemap" for row in result["tests"]))
        self.assertNotIn("https://unofficial.example/news/percy-jackson", calls)

    def test_sitemap_rejects_external_and_unbounded_indexes(self):
        alias = ["Percy Jackson"]
        ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        outside = f'<sitemapindex {ns}><sitemap><loc>https://evil.example/s.xml</loc></sitemap></sitemapindex>'
        links, failure = pa._sitemap_links(outside, ["press.disneyplus.com"], alias)
        self.assertEqual((links, failure), ([], None))
        children = "".join(f"<sitemap><loc>https://press.disneyplus.com/s{i}.xml</loc></sitemap>"
                           for i in range(pa.MAX_SITEMAP_CHILDREN + 1))
        links, failure = pa._sitemap_links(f"<sitemapindex {ns}>{children}</sitemapindex>",
                                           ["press.disneyplus.com"], alias)
        self.assertEqual((links, failure), ([], "SITEMAP_CHILD_LIMIT"))
        links, failure = pa._sitemap_links('<!DOCTYPE root><urlset/>',
                                           ["press.disneyplus.com"], alias)
        self.assertEqual((links, failure), ([], "INVALID_SITEMAP"))

    def test_report_ordering_is_deterministic_and_does_not_store_article_body(self):
        cases = [case(f"sort{i}", f"https://press.example.org/article/s{i}") for i in (2, 1, 3)]

        def fetch(url, domains):
            if "/index/" in url:
                name = url.rsplit("/", 1)[-1]
                return fetched(url, f"<html><title>Index</title><h1>Index</h1><a href='/article/s{name[-1]}'>Article</a></html>")
            return fetched(url, article(body="Test Series has been renewed for season 2. private body phrase"))

        one = pa.qualify({"schemaVersion": 1, "providers": [provider(cases)]}, fetcher=fetch, github_actions=False, today=dt.date(2026, 1, 1))
        two = pa.qualify({"schemaVersion": 1, "providers": [provider(list(reversed(cases)))]}, fetcher=fetch, github_actions=False, today=dt.date(2026, 1, 1))
        self.assertEqual(json.dumps(one, sort_keys=True), json.dumps(two, sort_keys=True))
        encoded = json.dumps(one)
        self.assertNotIn("private body phrase", encoded)
        self.assertNotIn("articleBody", encoded)

    def test_qualification_does_not_mutate_protected_live_files(self):
        snapshots = {path: path.read_bytes() for path in pa.PROTECTED_PATHS if path.exists()}
        item = case("readonly", "https://press.example.org/article/readonly")
        pa.qualify_provider(provider([item]), lambda url, domains: fetched(
            url, "<html><title>Index</title><h1>Index</h1></html>"), github_actions=False)
        self.assertEqual(snapshots, {path: path.read_bytes() for path in snapshots})

    def test_qualify_registry_all_is_scoped_to_four_v271_providers(self):
        providers = [provider([]) | {"provider": name} for name in ("FX", "HULU", "DISNEY_PLUS", "AMC", "PEACOCK_NBC", "STARZ", "HBO_MAX_WBD")]
        report = pa.qualify({"schemaVersion": 1, "providers": providers}, fetcher=lambda *args: (None, None), github_actions=False)
        self.assertEqual([row["provider"] for row in report["providers"]], ["AMC", "DISNEY_PLUS", "FX", "HULU"])


if __name__ == "__main__":
    unittest.main()
