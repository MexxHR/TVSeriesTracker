from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from unittest import mock

import provider_coverage_audit as audit


DOMAINS = {
    "NETFLIX": ["netflix.com"], "PARAMOUNT": ["paramountpressexpress.com"],
    "APPLE": ["apple.com"], "AMAZON": ["aboutamazon.com"], "AMC": ["amcglobalmedia.com"],
    "DISNEY_PLUS": ["press.disneyplus.com"], "HULU": ["press.hulu.com"],
    "HBO": ["press-spain.max.com"], "MAX": ["press-spain.max.com"], "FX": ["fxnetworks.com"],
}


def positive_case(provider="NETFLIX", case_id="n-1", *, known=None, seeded=False):
    domain = DOMAINS[provider][0]
    url = f"https://{domain}/press/example-season-2"
    facts = {"status": "RENEWED", "nextSeasonNumber": 2, "releaseDate": None,
             "releaseYear": None, "sourceName": "Official press release", "sourceUrl": url,
             "announcementDate": "2026-01-02"}
    return {"caseId": case_id, "kind": "positive", "goldReviewStatus": "VERIFIED",
            "tmdbId": 100 + list(DOMAINS).index(provider), "title": "Example Series", "aliases": ["Example Series"],
            "canonicalUrl": url, "independentEvidenceUrl": url,
            "independentRationale": "The first-party announcement explicitly confirms renewal for season two.",
            "expectedFacts": facts, "expectedFactKnownFields": known or list(audit.FACT_FIELDS),
            "releaseDateScope": "none", "curationProvenance": {"method": "primary_page_read",
                "parserOutputUsedAsGroundTruth": False},
            "manuallyCurated": True, "manuallySeeded": seeded}


def corpus():
    return {"schemaVersion": 1, "corpusVersion": "1", "curatedAt": "2026-10-14",
            "curationMethod": "independent_first_party_manual_review",
            "providers": [{"provider": name, "productionEnabled": name != "FX", "officialDomains": domains,
                           "cases": [positive_case(name, f"{name.lower()}-1")]}
                          for name, domains in DOMAINS.items()]}


class CorpusSchemaTests(unittest.TestCase):
    def test_schema_accepts_all_ten_required_providers(self):
        self.assertEqual(audit.validate_corpus(corpus())["corpusVersion"], "1")

    def test_duplicate_case_ids_rejected(self):
        data = corpus()
        data["providers"][1]["cases"][0]["caseId"] = data["providers"][0]["cases"][0]["caseId"]
        with self.assertRaises(audit.CorpusError):
            audit.validate_corpus(data)

    def test_duplicate_canonical_url_in_same_semantic_class_rejected(self):
        data = corpus()
        p = data["providers"][0]
        p["cases"].append(positive_case("NETFLIX", "n-2"))
        with self.assertRaisesRegex(audit.CorpusError, "duplicate canonical URL"):
            audit.validate_corpus(data)

    def test_same_url_different_semantic_class_is_allowed(self):
        data = corpus()
        p = data["providers"][0]
        negative = {**positive_case("NETFLIX", "n-neg"), "kind": "negative", "expectedFacts": None,
                    "semanticCaseClass": "casting", "expectedFactKnownFields": None}
        p["cases"].append(negative)
        self.assertEqual(len(audit.validate_corpus(data)["providers"][0]["cases"]), 2)

    def test_non_first_party_or_http_canonical_url_rejected(self):
        data = corpus()
        data["providers"][0]["cases"][0]["canonicalUrl"] = "http://thirdparty.example/post"
        with self.assertRaises(audit.CorpusError):
            audit.validate_corpus(data)

    def test_non_first_party_independent_evidence_rejected(self):
        data = corpus()
        data["providers"][0]["cases"][0]["independentEvidenceUrl"] = "https://example.org/review"
        with self.assertRaises(audit.CorpusError):
            audit.validate_corpus(data)

    def test_seven_fact_schema_and_season_required(self):
        data = corpus()
        data["providers"][0]["cases"][0]["expectedFacts"].pop("sourceName")
        with self.assertRaises(audit.CorpusError):
            audit.validate_corpus(data)

    def test_partial_gold_unknown_publication_date_is_preserved(self):
        data = corpus()
        case = data["providers"][0]["cases"][0]
        case["expectedFactKnownFields"] = [f for f in audit.FACT_FIELDS if f != "announcementDate"]
        case["expectedFacts"]["announcementDate"] = None
        audit.validate_corpus(data)

    def test_partial_gold_cannot_hide_status_or_season(self):
        data = corpus()
        data["providers"][0]["cases"][0]["expectedFactKnownFields"] = ["sourceUrl"]
        with self.assertRaises(audit.CorpusError):
            audit.validate_corpus(data)

    def test_manual_review_may_have_no_tmdb_id_or_facts(self):
        data = corpus()
        item = {"caseId": "manual-1", "kind": "manual_review", "goldReviewStatus": "MANUAL_REVIEW",
                "tmdbId": None, "expectedFacts": None, "manuallyCurated": True, "manuallySeeded": False,
                "curationProvenance": "awaiting reviewer"}
        data["providers"][0]["cases"].append(item)
        audit.validate_corpus(data)

    def test_fx_is_only_inactive_control(self):
        data = corpus()
        data["providers"][-1]["productionEnabled"] = True
        with self.assertRaises(audit.CorpusError):
            audit.validate_corpus(data)


class AccountingTests(unittest.TestCase):
    def _row(self, **overrides):
        row = {"kind": "positive", "denominatorEligible": True, "routeValid": True, "sourceAccessible": True,
               "genericDiscovery": True, "identityAccepted": True, "lifecycleParsed": True,
               "reconstructionPassed": True, "covered": True, "manuallySeeded": False,
               "directUrlDiagnostic": {"fetchable": True}, "primaryFailure": None}
        row.update(overrides)
        return row

    def test_funnel_counts_are_cumulative_and_monotonic(self):
        p = corpus()["providers"][0]
        rows = [self._row(routeValid=True), self._row(routeValid=True, genericDiscovery=False, identityAccepted=False,
                                       lifecycleParsed=False, reconstructionPassed=False, covered=False,
                                       primaryFailure="DISCOVERY_MISSED")]
        rollup = audit._provider_rollup(p, rows)
        self.assertEqual(rollup["funnel"], [2, 2, 2, 1, 1, 1, 1, 1])

    def test_inaccessible_case_is_separate_from_accessible_coverage(self):
        rows = [self._row(), self._row(sourceAccessible=False, genericDiscovery=False,
                                       identityAccepted=False, lifecycleParsed=False,
                                       reconstructionPassed=False, covered=False,
                                       primaryFailure="SOURCE_INACCESSIBLE")]
        rollup = audit._provider_rollup(corpus()["providers"][0], rows)
        self.assertEqual(rollup["rawE2EPercent"], 50)
        self.assertEqual(rollup["accessibleE2EPercent"], 100)

    def test_weighted_and_macro_coverage_remain_distinct(self):
        def provider(name, positives, covered):
            measured = positives
            return {"counts": {"goldEligible": positives, "sourceAccessible": positives, "covered": covered},
                    "measuredPositiveCases": measured,
                    "rawE2EPercent": audit._pct(covered, measured)}
        result = audit._aggregate_coverage([provider("small", 1, 1), provider("large", 9, 0)])
        self.assertEqual(result["weightedRawE2EPercent"], 10)
        self.assertEqual(result["macroProviderCoveragePercent"], 50)

    def test_macro_coverage_zero_denominator_is_not_applicable(self):
        result = audit._aggregate_coverage([])
        self.assertIsNone(result["weightedRawE2EPercent"])
        self.assertIsNone(result["macroProviderCoveragePercent"])

    def test_zero_denominator_returns_not_applicable(self):
        row = audit._provider_rollup(corpus()["providers"][0], [])
        self.assertIsNone(row["rawE2EPercent"])
        self.assertIsNone(row["directUrlPercent"])

    def test_failure_taxonomy_preserves_inaccessible_vs_corpus_invalid(self):
        from urllib.error import HTTPError
        self.assertEqual(audit.classify_exception(HTTPError("https://x", 403, "blocked", {}, None)), "SOURCE_INACCESSIBLE")
        self.assertEqual(audit.classify_exception(HTTPError("https://x", 404, "gone", {}, None)), "CORPUS_INVALID")
        self.assertEqual(audit.classify_exception(TimeoutError(), repeated=False), "NETWORK_INCONCLUSIVE")
        self.assertEqual(audit.classify_exception(TimeoutError(), repeated=True), "SOURCE_INACCESSIBLE")

    def test_negative_false_positive_accounting(self):
        p = corpus()["providers"][0]
        negative = {"kind": "negative", "denominatorEligible": True, "sourceAccessible": True,
                    "directUrlDiagnostic": {"fetchable": True}, "falsePositive": True,
                    "falsePositiveDenominatorEligible": True, "primaryFailure": "FALSE_POSITIVE"}
        result = audit._provider_rollup(p, [negative])
        self.assertEqual(result["falsePositives"], 1)
        self.assertEqual(result["falsePositiveRate"], 100)

    def test_manual_seed_does_not_count_as_generic_discovery_or_e2e(self):
        row = self._row(genericDiscovery=False, covered=False, manuallySeeded=True,
                        primaryFailure="MANUALLY_SEEDED_EXCLUDED")
        row["denominatorEligible"] = False
        result = audit._provider_rollup(corpus()["providers"][0], [row])
        self.assertEqual(result["genericDiscoveryCases"], 0)
        self.assertEqual(result["manuallySeededCases"], 1)
        self.assertIsNone(result["rawE2EPercent"])

    def test_known_url_only_is_diagnostic_only(self):
        row = self._row(genericDiscovery=False, covered=False, directUrlDiagnostic={"fetchable": True})
        result = audit._provider_rollup(corpus()["providers"][0], [row])
        self.assertEqual(result["knownUrlOnlyCases"], 1)
        self.assertEqual(result["discoveryPercent"], 0)

    def test_confidence_small_corpus_or_unknown_gold_is_low(self):
        p = corpus()["providers"][0]
        cases = [self._row(expectedFactKnownFields=["status", "nextSeasonNumber"]) for _ in range(8)]
        self.assertEqual(audit._provider_rollup(p, cases)["confidence"], "LOW")

    def test_roi_ranking_is_deterministic_and_excludes_fx(self):
        rows = [{"provider": "NETFLIX", "productionEnabled": True, "measuredPositiveCases": 4,
                "counts": {"sourceAccessible": 4, "covered": 1},
                 "confidence": "MEDIUM", "positiveFailureBreakdown": {"DISCOVERY_MISSED": 3}},
                {"provider": "FX", "productionEnabled": False, "measuredPositiveCases": 4,
                 "counts": {"sourceAccessible": 4, "covered": 0},
                 "confidence": "HIGH", "positiveFailureBreakdown": {"SOURCE_INACCESSIBLE": 4}}]
        self.assertEqual(audit._roi(rows), audit._roi(rows))
        self.assertEqual([r["provider"] for r in audit._roi(rows)], ["NETFLIX"])

    def test_protected_read_only_scope_is_explicit(self):
        self.assertEqual(len(audit.PROTECTED), 5)
        self.assertIn("official-data/official_series_data.json", audit.PROTECTED)


class DiscoveryBoundaryTests(unittest.TestCase):
    def test_tmdb_route_uses_server_token_and_checks_identity(self):
        seen = {}
        def fake(url, domains, headers):
            seen.update(url=url, domains=domains, headers=headers)
            return '{"id":101,"name":"Example"}', url
        meta = audit._fresh_metadata(101, "test-only-token", fake)
        self.assertEqual(meta["tmdbId"], 101)
        self.assertEqual(seen["headers"]["Authorization"], "Bearer test-only-token")
        self.assertEqual(seen["domains"], ["api.themoviedb.org"])

    def test_tmdb_identity_mismatch_rejected(self):
        with self.assertRaises(Exception):
            audit._fresh_metadata(101, "token", lambda *args: ('{"id":102,"name":"Wrong"}', "https://api.themoviedb.org"))

    def test_oracle_and_generic_caches_are_independent(self):
        calls = []
        def transport(url, domains):
            calls.append(url)
            return "raw", url
        oracle = audit.BoundedFetcher("NETFLIX", transport=transport, max_requests=1)
        generic = audit.BoundedFetcher("NETFLIX", transport=transport, max_requests=2)
        oracle("https://netflix.com/post/gold", DOMAINS["NETFLIX"])
        generic("https://netflix.com/post/gold", DOMAINS["NETFLIX"])
        self.assertEqual(calls, ["https://netflix.com/post/gold"] * 2)

    def test_direct_article_fetch_cannot_count_as_generic_discovery(self):
        provider = corpus()["providers"][0]
        case = provider["cases"][0]
        calls = []
        def transport(url, domains, headers=None):
            calls.append(url)
            if "api.themoviedb.org" in domains:
                return '{"id":100,"name":"Example Series","networks":[{"name":"Netflix"}]}', url
            return "first-party diagnostic body", url
        ordinary = audit.BoundedFetcher("NETFLIX", transport=transport)
        diagnostic = audit.BoundedFetcher("NETFLIX", transport=transport)
        with mock.patch.object(audit.discovery, "route_provider", return_value=("NETFLIX", ["networks:Netflix"], None)), \
             mock.patch.object(audit.discovery, "discover", return_value={"result": "insufficient_evidence", "candidates": []}) as discover:
            with mock.patch.object(audit, "_run_pipeline", return_value={"pipelineState": "NO_VERIFIED_FACTS",
                    "parserResult": None, "persisted": False, "verifiedFacts": {},
                    "reconstructionPassed": False, "reconstructedFacts": None, "errorType": None}):
                row = audit._case_result(provider, case, token="unit-fixture-token", specs={"NETFLIX": {"candidateDomains": DOMAINS["NETFLIX"]}},
                                         transport=transport, provider_fetcher=ordinary,
                                         diagnostic_fetcher=diagnostic)
        self.assertTrue(row["directUrlDiagnostic"]["fetchable"])
        self.assertFalse(row["genericDiscovery"])
        self.assertFalse(row["covered"])
        self.assertEqual(row["primaryFailure"], "DISCOVERY_MISSED")
        self.assertEqual(discover.call_args.args[1], {"schemaVersion": 1, "series": []})
        self.assertNotIn(case["canonicalUrl"], discover.call_args.kwargs)
        self.assertEqual(calls.count(case["canonicalUrl"]), 1)

    def test_wrong_candidate_for_same_series_does_not_count_as_gold_discovery(self):
        provider = corpus()["providers"][0]
        case = provider["cases"][0]
        wrong = {"candidateUrl": "https://netflix.com/press/unrelated-season-2", "factualParserEligible": True,
                 "identityValidated": True, "providerRoutingConfidence": "NETWORK"}
        transport = lambda url, domains, headers=None: ('{"id":100,"name":"Example Series","networks":[{"name":"Netflix"}]}' if headers else "article", url)
        with mock.patch.object(audit.discovery, "route_provider", return_value=("NETFLIX", ["networks:Netflix"], None)), \
             mock.patch.object(audit.discovery, "discover", return_value={"result": "candidate_found", "candidates": [wrong]}), \
             mock.patch.object(audit, "_run_pipeline") as run:
            row = audit._case_result(provider, case, token="token", specs={"NETFLIX": {"candidateDomains": DOMAINS["NETFLIX"]}}, transport=transport)
        self.assertFalse(row["genericDiscovery"])
        self.assertEqual(row["primaryFailure"], "DISCOVERY_MISSED")
        self.assertTrue(run.called)
        self.assertTrue(run.call_args.kwargs["direct_diagnostic"])

    def test_corpus_404_is_excluded_from_denominator_before_route_or_discovery(self):
        from urllib.error import HTTPError
        provider = corpus()["providers"][0]
        case = provider["cases"][0]
        def transport(url, domains, headers=None):
            if headers is not None:
                return '{"id":100,"name":"Example Series","networks":[{"name":"Netflix"}]}', url
            raise HTTPError(url, 404, "gone", {}, None)
        with mock.patch.object(audit.discovery, "route_provider", return_value=("NETFLIX", ["networks:Netflix"], None)) as route, mock.patch.object(audit.discovery, "discover") as discover:
            row = audit._case_result(provider, case, token="token", specs={"NETFLIX": {"candidateDomains": DOMAINS["NETFLIX"]}}, transport=transport)
        self.assertEqual(row["primaryFailure"], "CORPUS_INVALID")
        self.assertFalse(row["denominatorEligible"])
        self.assertEqual(row["httpStatus"], 404)
        self.assertTrue(route.called)
        self.assertFalse(discover.called)

    def test_negative_false_positive_uses_direct_parser_diagnostic(self):
        provider = corpus()["providers"][0]
        case = {**provider["cases"][0], "caseId": "negative", "kind": "negative",
                "expectedFacts": None, "expectedFactKnownFields": None}
        output = {"pipelineState": "VERIFIED_FACTS", "parserResult": "facts", "persisted": True,
                  "verifiedFacts": {"status": "RENEWED"}, "reconstructionPassed": True,
                  "reconstructedFacts": {"status": "RENEWED"}, "errorType": None}
        transport = lambda url, domains, headers=None: ('{"id":100,"name":"Example Series","networks":[{"name":"Netflix"}]}' if headers else "article", url)
        with mock.patch.object(audit.discovery, "route_provider", return_value=("NETFLIX", ["networks:Netflix"], None)), \
             mock.patch.object(audit.discovery, "discover", return_value={"result": "insufficient_evidence", "candidates": []}), \
             mock.patch.object(audit, "_run_pipeline", return_value=output) as run:
            row = audit._case_result(provider, case, token="token", specs={"NETFLIX": {"candidateDomains": DOMAINS["NETFLIX"]}}, transport=transport)
        self.assertTrue(row["falsePositive"])
        self.assertTrue(row["falsePositiveDenominatorEligible"])
        self.assertEqual(row["primaryFailure"], "FALSE_POSITIVE")
        self.assertEqual(run.call_args.kwargs["direct_diagnostic"], True)
        self.assertFalse(row["genericDiscovery"])

    def test_safe_query_identity_is_preserved_and_secret_values_redacted(self):
        self.assertEqual(audit._public_url("https://press.example/article?view=full&token=abcdefghijklmnop"),
                         "https://press.example/article?view=full&token=%5BREDACTED%5D")
        self.assertEqual(audit._canonical("https://press.example/article?view=full"),
                         audit._canonical("https://PRESS.EXAMPLE/article/?view=full"))

    def test_real_generic_to_parser_persist_and_independent_replay_path(self):
        provider = corpus()["providers"][0]
        case = provider["cases"][0]
        case["expectedFacts"]["sourceName"] = "Netflix Tudum"
        case["expectedFacts"]["announcementDate"] = None
        case["expectedFactKnownFields"] = [field for field in audit.FACT_FIELDS if field != "announcementDate"]
        candidate = {"candidateUrl": case["canonicalUrl"], "factualParserEligible": True,
                     "identityValidated": True, "providerRoutingConfidence": "NETWORK",
                     "discoveryMethod": "mocked_index_candidate"}
        article = ("<html><head><title>Example Series renewed for season two</title></head>"
                   "<body><h1>Example Series</h1><article><p>Example Series has been renewed for a second season.</p></article></body></html>")
        calls = []
        def transport(url, domains, headers=None):
            calls.append((url, tuple(domains)))
            if headers is not None:
                return '{"id":100,"name":"Example Series","networks":[{"name":"Netflix"}]}', url
            return article, url
        with mock.patch.object(audit.discovery, "route_provider", return_value=("NETFLIX", ["networks:Netflix"], None)), \
             mock.patch.object(audit.discovery, "discover", return_value={"provider": "NETFLIX", "result": "candidate_found", "candidates": [candidate]}):
            row = audit._case_result(provider, case, token="fixture-token", specs=audit.discovery.provider_specs(), transport=transport,
                                     today=dt.date(2026, 10, 14))
        self.assertTrue(row["genericDiscovery"])
        self.assertTrue(row["lifecycleParsed"])
        self.assertTrue(row["persistedRecord"])
        self.assertTrue(row["reconstructionPassed"])
        self.assertTrue(row["covered"])
        self.assertEqual(row["reconstructedFacts"]["status"], "RENEWED")
        self.assertGreaterEqual(sum(url == case["canonicalUrl"] for url, _ in calls), 2)


class ReportContractTests(unittest.TestCase):
    def _authority_report(self, *, actions, mode="local", token=None, measured=False):
        def case_result(provider, case, **kwargs):
            return audit._case_aliases({
                "caseId": case["caseId"], "provider": provider["provider"],
                "kind": case["kind"], "goldReviewStatus": case["goldReviewStatus"],
                "expectedFacts": case.get("expectedFacts"),
                "expectedFactKnownFields": case.get("expectedFactKnownFields"),
                "denominatorEligible": measured and case["kind"] == "positive" and provider["productionEnabled"],
                "freshMetadataFetched": measured and provider["productionEnabled"],
                "routeValid": measured, "sourceAccessible": measured,
                "genericDiscovery": False, "identityAccepted": False,
                "lifecycleParsed": False, "reconstructionPassed": False, "covered": False,
                "primaryFailure": "DISCOVERY_MISSED" if measured else "LOCAL_INCONCLUSIVE",
                "manuallySeeded": False, "secondaryFailures": [],
            }, case)
        with mock.patch.dict(audit.os.environ, {"GITHUB_ACTIONS": actions}), \
             mock.patch.object(audit, "_wbd_access_probe", return_value={"status": "TEST_FIXTURE"}), \
             mock.patch.object(audit, "_fx_control_audit", return_value={"status": "TEST_FIXTURE"}), \
             mock.patch.object(audit, "_case_result", side_effect=case_result):
            return audit.build_report(audit.load_corpus(), token=token, execution_mode=mode)

    def test_actions_environment_alone_does_not_authorize_offline_report(self):
        report = self._authority_report(actions="true")
        self.assertEqual(report["finalCoverageStatus"], "AWAITING_GITHUB_RUNNER_CONFIRMATION")
        self.assertEqual(report["diagnosticStatus"], "INCONCLUSIVE")
        self.assertEqual(report["resultScope"], "LOCAL_DIAGNOSTIC_ONLY")
        self.assertFalse(report["runnerAuthorityConfirmed"])
        self.assertTrue(report["readOnly"])
        self.assertFalse(report["productionMutation"])

    def test_measured_local_diagnostic_still_requires_runner_confirmation(self):
        for actions in ("true", "false"):
            with self.subTest(actions=actions):
                report = self._authority_report(actions=actions, token="fixture-token", measured=True)
                self.assertEqual(report["diagnosticStatus"], "MEASURED")
                self.assertEqual(report["finalCoverageStatus"], "AWAITING_GITHUB_RUNNER_CONFIRMATION")
                self.assertEqual(report["resultScope"], "LOCAL_DIAGNOSTIC_ONLY")

    def test_explicit_runner_execution_can_confirm_measured_valid_report(self):
        report = self._authority_report(actions="true", mode="runner-confirmation", token="fixture-token", measured=True)
        self.assertEqual(report["resultScope"], "RUNNER_CONFIRMATION")
        self.assertEqual(report["finalCoverageStatus"], "GITHUB_RUNNER_CONFIRMATION")
        self.assertTrue(report["runnerAuthorityConfirmed"])
        self.assertTrue(report["qualityGate"]["valid"])

    def test_requested_runner_without_measurement_remains_pending(self):
        report = self._authority_report(actions="true", mode="runner-confirmation")
        self.assertEqual(report["diagnosticStatus"], "INCONCLUSIVE")
        self.assertEqual(report["finalCoverageStatus"], "AWAITING_GITHUB_RUNNER_CONFIRMATION")
        self.assertEqual(report["resultScope"], "RUNNER_INCONCLUSIVE")
        self.assertFalse(report["runnerAuthorityConfirmed"])

    def test_invalid_runner_report_is_not_a_success_artifact(self):
        with mock.patch.object(audit, "credential_scan", return_value=1):
            report = self._authority_report(actions="true", mode="runner-confirmation", token="fixture-token", measured=True)
        self.assertEqual(report["finalCoverageStatus"], "AUDIT_INVALID")
        self.assertFalse(report["runnerAuthorityConfirmed"])
        self.assertNotEqual(report["resultScope"], "RUNNER_CONFIRMATION")
        self.assertFalse(report["qualityGate"]["valid"])
        self.assertEqual(report["recommendations"], [])
        with tempfile.TemporaryDirectory() as directory:
            paths = audit.write_report(report, audit.Path(directory))
            self.assertIn('"finalCoverageStatus": "AUDIT_INVALID"', paths[0].read_text())

    def test_runner_mode_requires_actions_environment_before_any_fetch(self):
        with mock.patch.dict(audit.os.environ, {"GITHUB_ACTIONS": "false"}), \
             mock.patch.object(audit, "_wbd_access_probe") as probe:
            with self.assertRaisesRegex(ValueError, "GitHub Actions"):
                audit.build_report(audit.load_corpus(), execution_mode="runner-confirmation")
        probe.assert_not_called()

    def test_final_safety_or_output_scan_failure_revokes_runner_authority(self):
        for output_failure in (False, True):
            with self.subTest(output_scan=output_failure):
                report = self._authority_report(actions="true", mode="runner-confirmation", token="fixture-token", measured=True)
                if not output_failure:
                    report["qualityGate"]["valid"] = False
                    report["finalCoverageStatus"] = report["status"] = "AUDIT_INVALID"
                with tempfile.TemporaryDirectory() as directory, \
                     mock.patch.object(audit, "credential_scan_texts", return_value=int(output_failure)):
                    paths = audit.write_report(report, audit.Path(directory))
                    self.assertFalse(report["runnerAuthorityConfirmed"])
                    self.assertEqual(report["resultScope"], "RUNNER_INCONCLUSIVE")
                    self.assertIn('"finalCoverageStatus": "AUDIT_INVALID"', paths[0].read_text())

    def test_cli_validation_failure_creates_no_fake_artifact(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(audit, "load_corpus", side_effect=audit.CorpusError("invalid fixture")):
            self.assertEqual(audit.main(["--output-dir", directory]), 2)
            self.assertEqual(list(audit.Path(directory).iterdir()), [])

    def test_cli_defaults_local_and_passes_explicit_runner_mode(self):
        for args, expected in (([], "local"), (["--execution-mode", "runner-confirmation"], "runner-confirmation")):
            with self.subTest(mode=expected), tempfile.TemporaryDirectory() as directory, \
                 mock.patch.object(audit, "load_corpus", return_value={}), \
                 mock.patch.object(audit.discovery, "local_tmdb_token", return_value=None), \
                 mock.patch.object(audit, "build_report", return_value={"auditQuality": {"valid": True}}) as build, \
                 mock.patch.object(audit, "write_report"), \
                 mock.patch.object(audit, "render_markdown", return_value=""):
                self.assertEqual(audit.main(["--output-dir", directory] + args), 0)
                self.assertEqual(build.call_args.kwargs["execution_mode"], expected)

    def test_offline_report_has_runner_gate_and_three_artifact_contract(self):
        data = corpus()
        negative = {**positive_case("NETFLIX", "n-neg"), "kind": "negative", "expectedFacts": None,
                    "expectedFactKnownFields": None, "semanticCaseClass": "press_about_other_series"}
        negative["canonicalUrl"] = "https://netflix.com/press/unrelated-series"
        negative["independentEvidenceUrl"] = negative["canonicalUrl"]
        data["providers"][0]["cases"].append(negative)
        with mock.patch.object(audit, "_wbd_access_probe", return_value={"status": "TEST_FIXTURE"}), \
             mock.patch.object(audit, "_fx_control_audit", return_value={"status": "TEST_FIXTURE"}):
            report = audit.build_report(data, token=None, now=dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc))
        self.assertTrue(report["readOnly"])
        self.assertEqual(report["auditVersion"], "2.7.16")
        self.assertEqual(report["finalCoverageStatus"], "AWAITING_GITHUB_RUNNER_CONFIRMATION")
        self.assertEqual(report["resultScope"], "LOCAL_DIAGNOSTIC_ONLY")
        self.assertTrue(report["qualityGate"]["valid"])
        self.assertTrue(report["qualityGate"]["falsePositiveCorpusPresent"])
        self.assertIsNone(report["providers"][0]["rawE2ECoveragePercent"])
        self.assertEqual(report["providers"][0]["rating"], "LOW_CONFIDENCE")
        self.assertTrue(report["failureRoiAnalysis"]["PRODUCTION_ONLY"])
        with tempfile.TemporaryDirectory() as directory:
            paths = audit.write_report(report, audit.Path(directory))
            self.assertEqual([path.name for path in paths], ["provider-coverage-audit.json",
                "provider-coverage-audit.md", "provider-coverage-cases.jsonl"])
            self.assertIn('"qualityGate"', paths[0].read_text(encoding="utf-8"))

    def test_bounded_fetcher_deduplicates_and_rejects_out_of_scope(self):
        calls = []
        fetcher = audit.BoundedFetcher("NETFLIX", transport=lambda u, d: (calls.append(u) or "raw", u))
        fetcher("https://www.netflix.com/title", DOMAINS["NETFLIX"])
        fetcher("https://www.netflix.com/title", DOMAINS["NETFLIX"])
        self.assertEqual(len(calls), 1)
        with self.assertRaises(Exception):
            fetcher("https://example.org/title", DOMAINS["NETFLIX"])

    def test_real_provider_route_is_obtained_from_fresh_metadata(self):
        specs = {"HBO": {"routingNames": ["HBO"], "officialDomains": DOMAINS["HBO"],
                          "candidateDomains": DOMAINS["HBO"], "seeds": [], "productionActivated": True},
                 "MAX": {"routingNames": ["Max"], "officialDomains": DOMAINS["MAX"],
                         "candidateDomains": DOMAINS["MAX"], "seeds": [], "productionActivated": True}}
        self.assertEqual(audit.discovery.route_provider({"tmdbId": 1, "title": "X", "networks": [{"name": "HBO"}]}, specs)[0], "HBO")
        self.assertIsNone(audit.discovery.route_provider({"tmdbId": 1, "title": "X", "networks": [{"name": "HBO"}, {"name": "Max"}]}, specs)[0])

    def test_independent_reconstruction_compares_all_seven_fields(self):
        actual = {field: None for field in audit.FACT_FIELDS}
        expected = dict(actual)
        expected["status"] = "RENEWED"
        self.assertEqual(audit._compare_facts(actual, expected), ["status"])


if __name__ == "__main__":
    unittest.main()
