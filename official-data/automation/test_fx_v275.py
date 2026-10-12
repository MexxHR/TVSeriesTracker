"""FX-only excerpt fixtures: synthetic transport, never runner confirmation.

Short official text excerpts inspected on 2026-10-12; HTML wrappers and
root links are constructed, not downloaded page snapshots. No network IO.
"""
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import fx_hulu_qualification as qualification
import provider_audit as audit

ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date(2026, 10, 12)
EXCERPTS = {
    "fx-lowdown-s2": ("FX’s The Lowdown Renewed for Second Season on FX and Hulu", ""),
    "fx-snowfall-final-s6": ("FX's Snowfall | Watch on Hulu",
                             "In the sixth and final season of FX’s Snowfall"),
    "fx-mayans-final-s5": ("FX's Mayans MC | Watch on Hulu",
                           "<h2>FINAL SEASON</h2><h2>Season 5</h2>"),
    "fx-far-cry-cast-negative": ('New Cast Additions to FX\'s "Far Cry"', ""),
}


def fx_registry():
    registry = audit.load_registry()
    return {**registry, "providers": [row for row in registry["providers"] if row["provider"] == "FX"]}


def fixture_pages(registry):
    pages = {}
    for case in registry["providers"][0]["tests"]:
        title, body = EXCERPTS[case["caseId"]]
        pages[case["articleUrl"]] = f"<html><title>{title}</title><h1>{title}</h1><main>{body}</main></html>"
        for root in case["discoveryRoots"]:
            pages[root] = pages.get(root, "<h1>Constructed official index</h1>") + f'<a href="{case["articleUrl"]}">{case["series"]}</a>'
    return pages


def fetched(url, raw):
    return ({"status": "fetched", "httpStatus": 200, "initialHttpStatus": 200,
             "finalStatus": 200, "finalUrl": url, "redirectCount": 0,
             "redirectsWithinBoundary": True, "contentType": "text/html",
             "responseBytes": len(raw.encode()), "failureCategory": None}, raw)


def local_run():
    registry = fx_registry()
    pages = fixture_pages(registry)
    def fetch(url, domains):
        if url not in pages:
            return ({"status": "failed", "failureCategory": "FIXTURE_NOT_CONFIGURED"}, None)
        return fetched(url, pages[url])
    real_qualify = audit.qualify
    def fx_only(_registry, **kwargs):
        return real_qualify(registry, **kwargs)
    with patch.object(audit, "qualify", side_effect=fx_only):
        report = qualification.run(root=ROOT, github_runner=False, fetcher=fetch, today=TODAY)
    report["fixtureNotice"] = "LOCAL FX ONLY. Constructed HTML/root links; HTTP 200 is simulated. Official excerpts are not raw snapshots. No GitHub runner or real discovery confirmation."
    report["fxExtractDiagnostics"] = [
        {"caseId": case["caseId"], "officialUrl": case["articleUrl"],
         "pageTitle": EXCERPTS[case["caseId"]][0], "relevantExtract": EXCERPTS[case["caseId"]][1],
         "expected": case.get("expected"), "httpStatusActual": None,
         "httpStatusActualNote": "Web extraction succeeded; numeric origin status unavailable. Fixture transport status is simulated."}
        for case in registry["providers"][0]["tests"]]
    return report


class FxV275Tests(unittest.TestCase):
    def test_excerpt_run_keeps_safe_failure_and_zero_announcement_count(self):
        report = local_run()
        fx = report["providers"]["FX"]
        self.assertEqual((fx["passedCases"], fx["totalCases"]), (3, 4))
        self.assertEqual(fx["configuredProviderRootAnnouncements"], 0)
        self.assertEqual(fx["distinctPassingProviderAnnouncements"], 0)
        self.assertEqual(fx["minimumDistinctProviderAnnouncements"], 3)
        self.assertFalse(fx["thresholdPossibleFromConfiguredCaseSet"])
        self.assertFalse(report["runner"]["githubActions"])
        cases = {case["caseId"]: case for case in fx["cases"]}
        for key in ("fx-lowdown-s2", "fx-snowfall-final-s6"):
            self.assertTrue(cases[key]["passed"])
            replay = cases[key]["independentReconstruction"]
            self.assertEqual(replay["result"], "passed")
            self.assertEqual(replay["mismatchedFields"], [])
            self.assertEqual(len(replay["fieldsCompared"]), 7)
        self.assertFalse(cases["fx-mayans-final-s5"]["passed"])
        self.assertEqual(cases["fx-mayans-final-s5"]["earliestFailureStage"], "parser")
        negative = cases["fx-far-cry-cast-negative"]
        self.assertTrue(negative["passed"])
        self.assertEqual(negative["parserResult"]["factCount"], 0)
        self.assertEqual(negative["sourceEvidence"], [])

    def test_multi_title_negation_speculation_and_cast_are_not_facts(self):
        samples = [
            ("Series B", "Series A returns for its fifth season. Series B will conclude with its final season."),
            ("Series B", "Series A's fifth season premieres tomorrow. The final season of Series B is coming."),
            ("Series A", "Series A and Series B return. Series C concludes with its sixth and final season."),
            ("Series B", "Series A and Series B return. Series C concludes with its sixth and final season."),
            ("Series A", "Series A is expected to return for a fifth season."),
            ("Series A", "Series A has not been renewed for a fifth season."),
            ("Series A", "Cast announced for season five of Series A."),
        ]
        for title, text in samples:
            with self.subTest(title=title, text=text):
                content = audit._qualification_content(f"<h1>{title}</h1><main><p>{text}</p></main>",
                    "https://www.fxnetworks.com/news/test", {"series": title, "aliases": [title]}, TODAY)
                self.assertEqual(content["facts"], [])

    def test_concludes_wording_is_conservatively_unsupported(self):
        content = audit._qualification_content(
            "<h1>Series C</h1><main><p>Series A and Series B return. Series C concludes with its sixth and final season.</p></main>",
            "https://www.fxnetworks.com/news/test", {"series": "Series C", "aliases": ["Series C"]}, TODAY)
        self.assertEqual(content["facts"], [])

    def test_supported_final_wording_binds_only_series_c(self):
        text = "Series A and Series B return. Series C's sixth and final season begins now."
        for title in ("Series A", "Series B", "Series C"):
            content = audit._qualification_content(f"<h1>{title}</h1><main><p>{text}</p></main>",
                "https://www.fxnetworks.com/news/test", {"series": title, "aliases": [title]}, TODAY)
            self.assertEqual([(f["status"], f["season"]) for f in content["facts"]],
                             [("FINAL_SEASON", 6)] if title == "Series C" else [])

    def test_wrong_first_party_domain_cannot_be_discovered(self):
        registry = fx_registry()["providers"][0]
        case = dict(registry["tests"][0], articleUrl="https://fxnetworks.com.evil.example/renewal")
        registry = dict(registry, tests=[case])
        result = audit.qualify_provider(registry, lambda url, domains: fetched(url,
            '<a href="https://fxnetworks.com.evil.example/renewal">The Lowdown renewed season 2</a>'),
            github_actions=False, today=TODAY)
        self.assertFalse(result["tests"][0]["passed"])

    def test_fx_reconstruction_rejects_each_of_seven_field_mismatches(self):
        original = qualification.promotion._facts_summary
        for field in qualification.monitored.FACT_FIELDS:
            with self.subTest(field=field):
                def changed(*args, **kwargs):
                    summary = dict(original(*args, **kwargs))
                    summary[field] = "deliberate-fixture-mismatch"
                    return summary
                with patch.object(qualification.promotion, "_facts_summary", side_effect=changed):
                    report = local_run()
                case = next(c for c in report["providers"]["FX"]["cases"] if c["caseId"] == "fx-lowdown-s2")
                self.assertFalse(case["passed"])
                self.assertEqual(case["earliestFailureStage"], "independent_reconstruction")
                self.assertEqual(case["independentReconstruction"]["mismatchedFields"], [field])


if __name__ == "__main__":
    unittest.main()
