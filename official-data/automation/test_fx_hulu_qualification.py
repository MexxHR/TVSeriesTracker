"""Tests for the FX/Hulu read-only qualification wrapper."""
from pathlib import Path
import sys
import tempfile
import unittest
import datetime as dt
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import fx_hulu_qualification as qualification
import provider_audit


def fake_provider(provider: str, cases: list[dict], *, announcements: int = 1) -> dict:
    audit_cases = []
    configs = []
    for index, case in enumerate(cases):
        case_id = f"{provider.lower()}-{index + 1}"
        config = {"caseId": case_id, "kind": case.get("kind", "positive"),
                  "rootScope": case.get("rootScope", "provider"),
                  "sourceStructure": case.get("sourceStructure", "announcement"),
                  "series": case.get("series", f"Series {index + 1}"),
                  "articleUrl": f"https://example.test/{case_id}"}
        configs.append(config)
        audit_cases.append({"caseId": case_id, "kind": config["kind"], "series": config["series"],
                            "rootScope": config["rootScope"], "sourceStructure": config["sourceStructure"],
                            "articleUrl": config["articleUrl"], "discoveryRoots": [], "http": [],
                            "knownArticleDiagnostic": {"fetchable": False},
                            "content": {"pageTitle": config["series"], "facts": [], "signals": [],
                                        "parserFactCount": 0},
                            "discoveredFromRoot": bool(case.get("discovered", True)),
                            "discoveredUrl": config["articleUrl"] if case.get("discovered", True) else None,
                            "discoveredFinalUrl": config["articleUrl"] if case.get("discovered", True) else None,
                            "discoveryProvenance": "official_link_traversal", "parserCompatible": False,
                            "passed": bool(case.get("passed", True)),
                            "failureCategory": case.get("failureCategory")})
    positive_articles = {row["articleUrl"] for row in audit_cases[:announcements]}
    audit_row = {"provider": provider, "recommendation": "FETCHABLE_BUT_DISCOVERY_NEEDS_WORK",
                 "officialDomains": ["example.test"], "passedCaseCount": sum(row["passed"] for row in audit_cases),
                 "caseCount": len(audit_cases), "qualificationReady": False,
                 "tests": audit_cases}
    registry_row = {"provider": provider, "officialDomains": ["example.test"], "tests": configs}
    return registry_row, audit_row, positive_articles


class FxHuluQualificationTests(unittest.TestCase):
    def protected_root(self, directory: str) -> Path:
        root = Path(directory)
        for relative in qualification.PROTECTED_FILES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("fixture:" + relative).encode())
        return root

    def test_report_only_selects_fx_and_hulu_and_preserves_three_announcement_threshold(self):
        fx_registry, fx_audit, _ = fake_provider("FX", [
            {"series": "The Lowdown"}, {"series": "Mayans M.C.", "sourceStructure": "series_page"},
            {"series": "Snowfall", "sourceStructure": "series_page"}], announcements=1)
        hulu_registry, hulu_audit, _ = fake_provider("HULU", [
            {"series": "Difficult People"}, {"series": "Shrill"}, {"series": "Reasonable Doubt", "kind": "negative"}], announcements=3)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = self.protected_root(directory)
            registry = {"schemaVersion": 1, "providers": [fx_registry, hulu_registry]}

            def fake_qualify(_registry, **kwargs):
                self.assertEqual({item["provider"] for item in _registry["providers"]}, {"FX", "HULU"})
                return {"schemaVersion": 1, "reportType": "provider_discovery_qualification",
                        "environment": {"githubActions": True}, "providers": [fx_audit, hulu_audit]}

            with patch.object(provider_audit, "load_registry", return_value=registry), \
                    patch.object(provider_audit, "qualify", side_effect=fake_qualify):
                report = qualification.run(root=root, github_runner=True)
        self.assertEqual(set(report["providers"]), {"FX", "HULU"})
        self.assertEqual(report["providers"]["FX"]["configuredProviderRootAnnouncements"], 1)
        self.assertEqual(report["providers"]["FX"]["distinctPassingProviderAnnouncements"], 0)
        self.assertFalse(report["providers"]["FX"]["thresholdPossibleFromConfiguredCaseSet"])
        self.assertNotEqual(report["providers"]["FX"]["qualificationState"], "QUALIFIED_ON_GITHUB_RUNNER")
        self.assertFalse(report["summary"]["fxReadyForProductionActivation"])
        self.assertFalse(report["summary"]["huluReadyForProductionActivation"])
        self.assertTrue(report["protectedFileIntegrity"]["unchanged"])
        self.assertIsNone(report["providers"]["FX"]["cases"][0]["tmdbId"])
        self.assertEqual(report["providers"]["HULU"]["cases"][2]["classification"], "NEGATIVE_SAFETY_CASE_PASSED")
        self.assertEqual(report["providers"]["HULU"]["cases"][2]["validationResult"], "passed")
        self.assertIsNone(report["providers"]["HULU"]["cases"][2]["earliestFailureStage"])
        self.assertEqual(report["providers"]["HULU"]["passedCases"], 1)

    def test_protected_file_mutation_fails_the_report(self):
        fx_registry, fx_audit, _ = fake_provider("FX", [{"series": "The Lowdown"}])
        hulu_registry, hulu_audit, _ = fake_provider("HULU", [{"series": "Deli Boys"}])
        registry = {"schemaVersion": 1, "providers": [fx_registry, hulu_registry]}

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = self.protected_root(directory)
            def mutate(*args, **kwargs):
                (root / qualification.PROTECTED_FILES[0]).write_bytes(b"changed")
                return {"providers": [fx_audit, hulu_audit]}
            with patch.object(provider_audit, "load_registry", return_value=registry), \
                    patch.object(provider_audit, "qualify", side_effect=mutate):
                with self.assertRaisesRegex(RuntimeError, "Protected Official Data bytes changed"):
                    qualification.run(root=root, github_runner=True)

    def test_ambiguity_case_does_not_pass_when_reconstruction_mismatches(self):
        audit_case = {"caseId": "hulu-deli-boys-s2", "kind": "ambiguity", "series": "Deli Boys",
                      "articleUrl": "https://press.hulu.com/pressrelease/deli-boys", "discoveredFromRoot": True,
                      "discoveredUrl": "https://press.hulu.com/pressrelease/deli-boys",
                      "discoveredFinalUrl": "https://press.hulu.com/pressrelease/deli-boys",
                      "discoveryRoots": [], "discoveryProvenance": "official_link_traversal",
                      "knownArticleDiagnostic": {"fetchable": True}, "http": [],
                      "content": {"extractable": True, "pageTitle": "Deli Boys", "parserFactCount": 1,
                                  "facts": [{"factType": "LIFECYCLE", "status": "RENEWED", "season": 2}],
                                  "signals": []}, "parserCompatible": True, "passed": True}
        with patch.object(qualification, "_fact_reconstruction", return_value={"result": "mismatch"}):
            result = qualification._case_result("HULU", {"officialDomains": ["press.hulu.com"]}, audit_case,
                                                 {"series": "Deli Boys"}, 1, dt.date(2026, 1, 1),
                                                 github_runner=True)
        self.assertFalse(result["passed"])
        self.assertEqual(result["classification"], "INDEPENDENT_RECONSTRUCTION_MISMATCH")
        self.assertEqual(result["earliestFailureStage"], "independent_reconstruction")

    def test_independent_reconstruction_compares_all_seven_promotion_fields(self):
        expected = {"status": "RENEWED", "nextSeasonNumber": 2, "releaseDate": None,
                    "releaseYear": None, "sourceName": "Provider discovery qualification",
                    "sourceUrl": "https://press.hulu.com/pressrelease/difficult-people-s2",
                    "announcementDate": None}
        fact = {"factType": "LIFECYCLE", "rule": "renewal", "status": "RENEWED",
                "nextSeasonNumber": 2, "releaseDate": None, "releaseYear": None,
                "sourceName": expected["sourceName"], "sourceUrl": expected["sourceUrl"],
                "announcementDate": None, "evidenceText": "Difficult People has been renewed for season 2."}
        raw = ("<html><head><title>Difficult People Renewed</title></head><body>"
               "<h1>Difficult People Renewed</h1><article><p>Difficult People has been renewed for season 2.</p>"
               "</article></body></html>")
        fetcher = lambda _url, _domains: ({"status": "fetched", "finalUrl": expected["sourceUrl"]}, raw)
        case = {"discoveredUrl": expected["sourceUrl"], "discoveredFinalUrl": expected["sourceUrl"],
                "series": "Difficult People", "aliases": ["Difficult People"]}
        with patch("fx_hulu_qualification.update.detect", return_value=[fact]), \
                patch("fx_hulu_qualification.update.validate_facts"), \
                patch("fx_hulu_qualification.update.merge", return_value=(expected, [])), \
                patch("fx_hulu_qualification.promotion._validate_evidence", return_value=[fact]), \
                patch("fx_hulu_qualification.promotion._facts_summary", return_value=expected):
            result = qualification._fact_reconstruction(case, 1, ["press.hulu.com"],
                                                        dt.date(2026, 1, 1), fetcher)
        self.assertEqual(result["result"], "passed")
        self.assertEqual(result["fieldsCompared"], ["status", "nextSeasonNumber", "releaseDate", "releaseYear",
                                                     "sourceName", "sourceUrl", "announcementDate"])
        self.assertIsNone(result["tmdbId"])
        self.assertTrue(result["internalSyntheticKeyUsed"])

    def test_exact_field_mismatch_is_not_normalized_away(self):
        expected = {"status": "RENEWED", "nextSeasonNumber": 2, "releaseDate": None,
                    "releaseYear": None, "sourceName": "Provider discovery qualification",
                    "sourceUrl": "https://press.hulu.com/pressrelease/difficult-people-s2",
                    "announcementDate": None}
        fact = {"factType": "LIFECYCLE", "rule": "renewal", **expected,
                "evidenceText": "Difficult People has been renewed for season 2."}
        raw = "<html><title>Difficult People</title><body><h1>Difficult People</h1><p>renewed season 2</p></body></html>"
        fetcher = lambda _url, _domains: ({"status": "fetched", "finalUrl": expected["sourceUrl"]}, raw)
        case = {"discoveredUrl": expected["sourceUrl"], "series": "Difficult People"}
        wrong = dict(expected, sourceUrl="https://press.hulu.com/other")
        with patch("fx_hulu_qualification.update.detect", return_value=[fact]), \
                patch("fx_hulu_qualification.update.validate_facts"), \
                patch("fx_hulu_qualification.update.merge", return_value=(expected, [])), \
                patch("fx_hulu_qualification.promotion._validate_evidence", return_value=[fact]), \
                patch("fx_hulu_qualification.promotion._facts_summary", return_value=wrong):
            result = qualification._fact_reconstruction(case, 1, ["press.hulu.com"],
                                                        dt.date(2026, 1, 1), fetcher)
        self.assertEqual(result["result"], "mismatch")
        self.assertEqual(result["mismatchedFields"], ["sourceUrl"])

    def test_real_promotion_replay_accepts_a_strict_hulu_fixture(self):
        url = "https://press.hulu.com/pressrelease/difficult-people-season-two"
        raw = ("<html><head><title>Difficult People Renewed</title></head><body>"
               "<h1>Difficult People Renewed</h1><article><p>Difficult People has been renewed for season 2.</p>"
               "</article></body></html>")
        fetcher = lambda requested, domains: (
            {"status": "fetched", "finalUrl": url} if requested == url else {"status": "failed"},
            raw if requested == url else None)
        case = {"discoveredUrl": url, "discoveredFinalUrl": url,
                "series": "Difficult People", "aliases": ["Difficult People"]}
        result = qualification._fact_reconstruction(case, 1, ["press.hulu.com"], dt.date(2026, 1, 1), fetcher)
        self.assertEqual(result["result"], "passed")
        self.assertEqual(result["mismatchedFields"], [])
        self.assertEqual(result["verifiedFacts"]["status"], "RENEWED")
        self.assertIsNone(result["tmdbId"])
        self.assertTrue(result["internalSyntheticKeyUsed"])
        self.assertEqual(result["sourceEvidence"][0]["sourceUrl"], url)

    def test_cli_rejects_artifact_paths_inside_repository(self):
        destination = qualification.update.ROOT / "official-data" / "official_series_data.json"
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            qualification.main(["--output", str(destination)])

    def test_apostrophe_forms_and_identity_collision_boundary(self):
        base = "https://press.hulu.com/news/"
        html = ("<a href='/pressrelease/handmaids-tale-renewed'>The Handmaid's Tale renewed</a>"
                "<a href='/pressrelease/handmaids-tale-curly'>The Handmaid’s Tale renewal</a>"
                "<a href='/pressrelease/unrelated-handmaid'>Handmaid biography</a>"
                "<a href='https://press.hulu.com.evil.example/handmaids-tale'>The Handmaid’s Tale</a>")
        links = provider_audit._qualification_links(
            html, base, ["press.hulu.com"], ["The Handmaid's Tale", "The Handmaid’s Tale"],
            "https://press.hulu.com/pressrelease/known-diagnostic-only")
        self.assertIn("https://press.hulu.com/pressrelease/handmaids-tale-renewed", links)
        self.assertIn("https://press.hulu.com/pressrelease/handmaids-tale-curly", links)
        # The bounded link ranker can propose a one-token collision. It must
        # fail the later full-title identity check before becoming evidence.
        collision = {"pageTitle": "Handmaid: A Biography", "facts": [],
                     "parserFactCount": 0, "signals": []}
        safe, reason = provider_audit._expected_facts_match(
            collision, {"kind": "positive", "expected": {"titleMarkers": ["The Handmaid's Tale"]}})
        self.assertFalse(safe)
        self.assertEqual(reason, "ARTICLE_IDENTITY_MISMATCH")
        self.assertFalse(any("evil.example" in url for url in links))


if __name__ == "__main__":
    unittest.main()
