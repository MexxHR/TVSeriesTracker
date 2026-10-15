"""Focused deterministic tests for the audit-only fidelity evaluator."""
from __future__ import annotations

import copy
import datetime as dt
import unittest

import provider_coverage_fidelity as fidelity


TODAY = dt.date(2026, 10, 15)
PROVIDER = "NETFLIX"
TITLE = "Example Series"
TMDB_ID = 7654321


def fact(*, season=2, status="RENEWED", url="https://www.netflix.com/tudum/example-season-2",
         announced="2024-02-01", release_date=None, release_year=None):
    return {"status": status, "nextSeasonNumber": season, "releaseDate": release_date,
            "releaseYear": release_year, "sourceName": "Netflix Pressroom", "sourceUrl": url,
            "announcementDate": announced}


def proof(value, *, replay_type="LIFECYCLE", provider=PROVIDER, tmdb_id=TMDB_ID,
          title=TITLE, mutate=None):
    replayed = {"factType": replay_type, "tmdbId": tmdb_id, "title": title,
                "status": value["status"], "nextSeasonNumber": value["nextSeasonNumber"],
                "sourceUrl": value["sourceUrl"], "sourceName": value["sourceName"],
                "announcementDate": value["announcementDate"]}
    replayed_facts = [replayed]
    if value.get("status") == "RELEASE_DATE_CONFIRMED":
        replayed["status"] = "RENEWED"
        replayed_facts.append({"factType": "RELEASE_DATE", "tmdbId": tmdb_id, "title": title,
                               "status": "RELEASE_DATE_CONFIRMED",
                               "nextSeasonNumber": value["nextSeasonNumber"],
                               "releaseDate": value["releaseDate"], "releaseYear": value["releaseYear"],
                               "sourceUrl": value["sourceUrl"], "sourceName": value["sourceName"],
                               "announcementDate": value["announcementDate"]})
    if mutate:
        mutate(replayed)
    result = {"provider": provider, "tmdbId": tmdb_id, "identityAccepted": True,
              "routeValidated": True, "verifiedFacts": copy.deepcopy(value),
              "reconstructedFacts": copy.deepcopy(value), "replayedFacts": replayed_facts,
              "factsCompared": list(fidelity.FACT_FIELDS), "passed": True,
              "sourceEvidence": [{"sourceUrl": value["sourceUrl"],
                                  "sourceName": value["sourceName"],
                                  "announcementDate": value["announcementDate"],
                                  "factType": "LIFECYCLE"}],
              "providerIdentity": {"provider": provider, "tmdbId": tmdb_id, "title": title}}
    return result


def setup_rows(gold=None, current_fact=None, *, replay_type="LIFECYCLE", current_proof_mutate=None):
    gold = gold or fact()
    current_fact = current_fact or copy.deepcopy(gold)
    case = {"caseId": "generic-fixture", "tmdbId": TMDB_ID, "title": TITLE,
            "kind": "positive", "goldReviewStatus": "VERIFIED",
            "expectedFacts": copy.deepcopy(gold), "expectedFactKnownFields": list(fidelity.FACT_FIELDS),
            "releaseDateScope": "global"}
    historical_fact = copy.deepcopy(gold)
    current = {"genericDiscovery": True, "routeValid": True, "identityAccepted": True,
               "lifecycleParsed": True, "reconstructionPassed": True,
               "actualFacts": copy.deepcopy(current_fact),
               "reconstructedFacts": copy.deepcopy(current_fact),
               "reconstructionProof": proof(current_fact, replay_type=replay_type, mutate=current_proof_mutate),
               "directUrlDiagnostic": {"actualFacts": historical_fact,
                                        "reconstructionProof": proof(historical_fact)}}
    legacy = {"covered": True, "primaryFailure": None,
              "discoveredGenerically": True, "reconstructionPassed": False,
              "customLegacyField": {"preserve": True}}
    return case, legacy, current


def run_eval(case, legacy, current, *, mode="PRODUCTION_EQUIVALENT_CAPTURE"):
    return fidelity.evaluate(case, legacy, current, provider=PROVIDER,
                             identity_context_mode=mode, today=TODAY)


class ProviderCoverageFidelityTests(unittest.TestCase):
    def test_exact_historical_match_is_covered(self):
        case, legacy, current = setup_rows()
        result = run_eval(case, legacy, current)
        self.assertEqual(result["currentCoverage"], "COVERED")
        self.assertTrue(result["historicalExactMatch"])
        self.assertEqual(result["auditFidelityFinding"], "NONE")

    def test_canonical_url_equivalence_allows_safe_presentation_changes(self):
        gold = fact(url="https://WWW.NETFLIX.com/tudum/example%2Dseason%2D2/")
        current_fact = fact(url="https://www.netflix.com/tudum/example-season-2?utm_source=mail")
        case, legacy, current = setup_rows(gold, current_fact)
        # Historical expected evidence and direct replay share the gold URL.
        result = run_eval(case, legacy, current)
        self.assertEqual(result["evidenceRelation"], "CANONICAL_EQUIVALENT")
        self.assertEqual(result["auditFidelityFinding"], "EQUIVALENT_EVIDENCE")

    def test_identity_query_parameter_is_not_removed(self):
        self.assertNotEqual(fidelity._canonical_url("https://www.netflix.com/story?view=1"),
                            fidelity._canonical_url("https://www.netflix.com/story?view=2"))

    def test_second_valid_first_party_announcement_is_equivalent(self):
        gold = fact()
        current_fact = fact(url="https://www.netflix.com/tudum/another-valid-story", announced="2025-01-09")
        case, legacy, current = setup_rows(gold, current_fact)
        result = run_eval(case, legacy, current)
        self.assertEqual(result["evidenceRelation"], "FIRST_PARTY_EQUIVALENT")
        self.assertEqual(result["temporalRelation"], "SAME")
        self.assertEqual(result["currentCoverage"], "COVERED")

    def test_later_season_renewal_supersedes_prior_season(self):
        case, legacy, current = setup_rows(fact(season=2), fact(season=3, url="https://www.netflix.com/tudum/season-3",
                                                                announced="2025-04-01"))
        result = run_eval(case, legacy, current)
        self.assertEqual(result["temporalRelation"], "SUPERSEDED")
        self.assertEqual(result["evidenceRelation"], "DIFFERENT_VALID_SUPERSEDING")
        self.assertEqual(result["auditFidelityFinding"], "HISTORICAL_GOLD_SUPERSEDED")

    def test_later_final_season_supersedes_earlier_renewal(self):
        case, legacy, current = setup_rows(fact(season=4), fact(season=5, status="FINAL_SEASON",
                                                               url="https://www.netflix.com/tudum/final-season",
                                                               announced="2025-05-02"))
        self.assertEqual(run_eval(case, legacy, current)["auditFidelityFinding"], "HISTORICAL_GOLD_SUPERSEDED")

    def test_newer_trailer_cannot_supply_lifecycle_replay(self):
        self._reject_non_lifecycle_article("TRAILER")

    def test_newer_teaser_cannot_supply_lifecycle_replay(self):
        self._reject_non_lifecycle_article("TEASER")

    def test_cast_announcement_cannot_supply_lifecycle_replay(self):
        self._reject_non_lifecycle_article("CAST")

    def test_ratings_article_cannot_supply_lifecycle_replay(self):
        self._reject_non_lifecycle_article("RATINGS")

    def test_interview_cannot_supply_lifecycle_replay(self):
        self._reject_non_lifecycle_article("INTERVIEW")

    def test_recap_cannot_supply_lifecycle_replay(self):
        self._reject_non_lifecycle_article("RECAP")

    def test_production_start_without_lifecycle_claim_cannot_supply_replay(self):
        self._reject_non_lifecycle_article("PRODUCTION_START")

    def test_wrong_title_replay_is_rejected(self):
        case, legacy, current = setup_rows(current_proof_mutate=lambda row: row.update(title="Different Show"))
        result = run_eval(case, legacy, current)
        self.assertNotEqual(result["currentCoverage"], "COVERED")

    def test_production_metadata_punctuation_variant_with_same_id_is_accepted(self):
        case, legacy, current = setup_rows()
        production_title = "Example Series…"
        proof_row = current["reconstructionProof"]
        proof_row["providerIdentity"]["title"] = production_title
        proof_row["replayedFacts"][0]["title"] = production_title
        result = run_eval(case, legacy, current)
        self.assertEqual(result["currentCoverage"], "COVERED")

    def test_replayed_fact_title_must_match_captured_metadata_title(self):
        case, legacy, current = setup_rows()
        current["reconstructionProof"]["providerIdentity"]["title"] = "Example Series…"
        current["reconstructionProof"]["replayedFacts"][0]["title"] = TITLE
        result = run_eval(case, legacy, current)
        self.assertNotEqual(result["currentCoverage"], "COVERED")

    def test_wrong_provider_is_rejected(self):
        case, legacy, current = setup_rows()
        current["reconstructionProof"]["provider"] = "APPLE"
        result = run_eval(case, legacy, current)
        self.assertNotEqual(result["currentCoverage"], "COVERED")

    def test_third_party_article_is_rejected(self):
        case, legacy, current = setup_rows(current_fact=fact(url="https://entertainment.example/story"))
        result = run_eval(case, legacy, current)
        self.assertNotEqual(result["currentCoverage"], "COVERED")

    def test_regional_date_does_not_become_global_fact(self):
        case, legacy, current = setup_rows(current_fact=fact(release_date="2027-01-02", release_year=2027))
        case["releaseDateScope"] = "regional"
        result = run_eval(case, legacy, current)
        self.assertNotEqual(result["currentCoverage"], "COVERED")

    def test_ambiguous_multiple_seasons_are_inconclusive(self):
        case, legacy, current = setup_rows(current_proof_mutate=lambda row: row.update(ambiguousSeason=True))
        self.assertNotEqual(run_eval(case, legacy, current)["currentCoverage"], "COVERED")

    def test_ambiguous_multiple_titles_are_inconclusive(self):
        case, legacy, current = setup_rows(current_proof_mutate=lambda row: row.update(ambiguousTitle=True))
        self.assertNotEqual(run_eval(case, legacy, current)["currentCoverage"], "COVERED")

    def test_production_equivalent_context_can_classify_generic_coverage(self):
        case, legacy, current = setup_rows()
        legacy.update(covered=False, primaryFailure="IDENTITY_REJECTED")
        current["identityContextComparison"] = {
            "different": True,
            "legacy": {"identityAccepted": False, "genericDiscovery": False},
            "productionEquivalent": {"identityAccepted": True, "genericDiscovery": True,
                                       "routeValid": True, "lifecycleParsed": True,
                                       "reconstructionPassed": True},
        }
        result = run_eval(case, legacy, current, mode="PRODUCTION_EQUIVALENT_CAPTURE")
        self.assertEqual(result["auditFidelityFinding"], "IDENTITY_CONTEXT_MISMATCH")

    def test_identity_context_mode_alone_does_not_relabel_failure(self):
        case, legacy, current = setup_rows()
        legacy.update(covered=False, primaryFailure="PARSER_FAILURE")
        result = run_eval(case, legacy, current, mode="PRODUCTION_EQUIVALENT_CAPTURE")
        self.assertEqual(result["auditFidelityFinding"], "NONE")

    def test_context_rule_is_generic_and_not_title_keyed(self):
        case, legacy, current = setup_rows()
        case["caseId"] = "unrelated-case-id"
        case["title"] = TITLE = "Another Arbitrary Series"
        for row in (current["reconstructionProof"]["replayedFacts"][0],
                    current["reconstructionProof"]["providerIdentity"],
                    current["directUrlDiagnostic"]["reconstructionProof"]["replayedFacts"][0],
                    current["directUrlDiagnostic"]["reconstructionProof"]["providerIdentity"]):
            row["title"] = TITLE
        legacy.update(covered=False, primaryFailure="IDENTITY_REJECTED")
        current["identityContextComparison"] = {
            "different": True,
            "legacy": {"identityAccepted": False, "genericDiscovery": False},
            "productionEquivalent": {"identityAccepted": True, "genericDiscovery": True,
                                       "routeValid": True, "lifecycleParsed": True,
                                       "reconstructionPassed": True},
        }
        self.assertEqual(run_eval(case, legacy, current)["auditFidelityFinding"], "IDENTITY_CONTEXT_MISMATCH")

    def test_historical_gold_is_immutable(self):
        case, legacy, current = setup_rows()
        before = copy.deepcopy(case["expectedFacts"])
        result = run_eval(case, legacy, current)
        result["historicalGold"]["status"] = "CANCELED"
        self.assertEqual(case["expectedFacts"], before)

    def test_current_fact_is_separate_from_historical_gold(self):
        case, legacy, current = setup_rows(fact(season=2), fact(season=3, url="https://www.netflix.com/tudum/season-3",
                                                                announced="2025-04-01"))
        result = run_eval(case, legacy, current)
        self.assertEqual(result["historicalGold"]["nextSeasonNumber"], 2)
        self.assertEqual(result["currentAuthoritativeFact"]["nextSeasonNumber"], 3)

    def test_legacy_fields_are_preserved(self):
        case, legacy, current = setup_rows()
        result = run_eval(case, legacy, current)
        for key in ("covered", "primaryFailure", "discoveredGenerically", "reconstructionPassed", "customLegacyField"):
            self.assertEqual(result[key], legacy[key])

    def test_generic_discovery_miss_remains_a_real_failure(self):
        case, legacy, current = setup_rows()
        current["genericDiscovery"] = False
        current["discoveredGenerically"] = False
        current["primaryFailure"] = "DISCOVERY_MISSED"
        result = run_eval(case, legacy, current)
        self.assertEqual(result["currentCoverage"], "NOT_COVERED")
        self.assertEqual(result["auditFidelityFinding"], "REAL_DISCOVERY_FAILURE")

    def test_source_unavailable_after_generic_discovery_is_inconclusive(self):
        case, legacy, current = setup_rows()
        legacy["primaryFailure"] = "LEGACY_FAILURE_PRESERVED"
        current["primaryFailure"] = "SOURCE_INACCESSIBLE"
        current["pipelineState"] = "PROVIDER_UNAVAILABLE"
        current["lifecycleParsed"] = False
        result = run_eval(case, legacy, current)
        self.assertEqual(result["currentCoverage"], "INCONCLUSIVE")
        self.assertEqual(result["auditFidelityFinding"], "INCONCLUSIVE")
        self.assertEqual(result["primaryFailure"], legacy["primaryFailure"])

    def test_network_secondary_failure_is_inconclusive_after_generic_discovery(self):
        case, legacy, current = setup_rows()
        current["secondaryFailures"] = [{"code": "NETWORK_INCONCLUSIVE"}]
        current["lifecycleParsed"] = False
        result = run_eval(case, legacy, current)
        self.assertEqual(result["currentCoverage"], "INCONCLUSIVE")
        self.assertEqual(result["auditFidelityFinding"], "INCONCLUSIVE")

    def test_network_metadata_failure_is_inconclusive(self):
        case, legacy, current = setup_rows()
        current["metadataFailure"] = "NETWORK: DNS lookup failed"
        current["genericDiscovery"] = False
        current["discoveredGenerically"] = False
        result = run_eval(case, legacy, current)
        self.assertEqual(result["currentCoverage"], "INCONCLUSIVE")
        self.assertEqual(result["auditFidelityFinding"], "INCONCLUSIVE")

    def test_local_no_generic_uncertainty_is_not_a_discovery_failure(self):
        case, legacy, current = setup_rows()
        current["primaryFailure"] = "LOCAL_INCONCLUSIVE"
        current["genericDiscovery"] = False
        current["discoveredGenerically"] = False
        result = run_eval(case, legacy, current)
        self.assertEqual(result["currentCoverage"], "INCONCLUSIVE")
        self.assertEqual(result["auditFidelityFinding"], "INCONCLUSIVE")

    def test_each_of_seven_current_fields_must_match_proof(self):
        for field, changed in (("status", "FINAL_SEASON"), ("nextSeasonNumber", 9),
                               ("releaseDate", "2028-01-01"), ("releaseYear", 2028),
                               ("sourceName", "Other"), ("sourceUrl", "https://www.netflix.com/elsewhere"),
                               ("announcementDate", "2025-01-01")):
            with self.subTest(field=field):
                case, legacy, current = setup_rows()
                current["actualFacts"][field] = changed
                current["reconstructedFacts"][field] = changed
                result = run_eval(case, legacy, current)
                self.assertNotEqual(result["currentCoverage"], "COVERED")

    def test_same_canonical_article_with_changed_publication_date_conflicts(self):
        gold = fact(url="https://www.netflix.com/tudum/example-season-2", announced="2024-02-01")
        newer = fact(url="https://www.netflix.com/tudum/example-season-2/", announced="2025-02-01")
        case, legacy, current = setup_rows(gold, newer)
        result = run_eval(case, legacy, current)
        self.assertEqual(result["evidenceRelation"], "CONFLICTING")
        self.assertEqual(result["auditFidelityFinding"], "INCONCLUSIVE")
        self.assertEqual(result["currentCoverage"], "NOT_COVERED")

    def test_same_canonical_url_changed_publication_date_cannot_supersede_later_season(self):
        gold = fact(season=2, url="https://www.netflix.com/tudum/series-story", announced="2024-02-01")
        later = fact(season=3, url="https://www.netflix.com/tudum/series-story/", announced="2025-02-01")
        case, legacy, current = setup_rows(gold, later)
        result = run_eval(case, legacy, current)
        self.assertEqual(result["evidenceRelation"], "CONFLICTING")
        self.assertEqual(result["currentCoverage"], "NOT_COVERED")
        self.assertNotEqual(result["auditFidelityFinding"], "HISTORICAL_GOLD_SUPERSEDED")

    def test_canonical_equivalence_requires_source_name_to_match(self):
        gold = fact(url="https://www.netflix.com/tudum/series-story")
        current_fact = fact(url="https://www.netflix.com/tudum/series-story/", announced=gold["announcementDate"])
        current_fact["sourceName"] = "Different source label"
        case, legacy, current = setup_rows(gold, current_fact)
        result = run_eval(case, legacy, current)
        self.assertEqual(result["evidenceRelation"], "CONFLICTING")
        self.assertEqual(result["currentCoverage"], "NOT_COVERED")

    def test_invalid_proof_flag_blocks_coverage(self):
        case, legacy, current = setup_rows()
        current["reconstructionProof"]["passed"] = False
        self.assertNotEqual(run_eval(case, legacy, current)["currentCoverage"], "COVERED")

    def test_out_of_range_publication_date_blocks_coverage(self):
        case, legacy, current = setup_rows(current_fact=fact(announced="2026-11-01"))
        self.assertNotEqual(run_eval(case, legacy, current)["currentCoverage"], "COVERED")

    def test_older_season_with_newer_article_is_not_supersession(self):
        case, legacy, current = setup_rows(fact(season=3), fact(season=2, announced="2025-04-01"))
        result = run_eval(case, legacy, current)
        self.assertEqual(result["temporalRelation"], "INCOMPARABLE")
        self.assertNotEqual(result["auditFidelityFinding"], "HISTORICAL_GOLD_SUPERSEDED")
        self.assertEqual(result["currentCoverage"], "NOT_COVERED")

    def test_equivalent_evidence_wins_when_contexts_are_identical(self):
        case, legacy, current = setup_rows(fact(), fact(url="https://www.netflix.com/tudum/valid-second-story",
                                                       announced="2025-01-02"))
        current["identityContextComparison"] = {
            "different": False,
            "legacy": {"aliases": [TITLE]},
            "productionEquivalent": {"aliases": [TITLE]},
        }
        legacy.update(covered=False, primaryFailure="PARSER_FAILURE")
        result = run_eval(case, legacy, current, mode="PRODUCTION_EQUIVALENT_CAPTURE")
        self.assertEqual(result["auditFidelityFinding"], "EQUIVALENT_EVIDENCE")
        self.assertEqual(result["currentCoverage"], "COVERED")

    def test_supersession_wins_when_contexts_are_identical(self):
        case, legacy, current = setup_rows(fact(season=4), fact(season=5, status="FINAL_SEASON",
                                                                url="https://www.netflix.com/tudum/final-season",
                                                                announced="2025-05-02"))
        current["identityContextComparison"] = {
            "different": False,
            "legacy": {"aliases": [TITLE]},
            "productionEquivalent": {"aliases": [TITLE]},
        }
        legacy.update(covered=False, primaryFailure="PARSER_FAILURE")
        result = run_eval(case, legacy, current, mode="PRODUCTION_EQUIVALENT_CAPTURE")
        self.assertEqual(result["auditFidelityFinding"], "HISTORICAL_GOLD_SUPERSEDED")
        self.assertEqual(result["currentCoverage"], "COVERED")

    def test_ambiguity_on_proof_object_blocks_coverage(self):
        case, legacy, current = setup_rows()
        current["reconstructionProof"]["ambiguousTitle"] = True
        result = run_eval(case, legacy, current)
        self.assertEqual(result["currentCoverage"], "INCONCLUSIVE")

    def test_ambiguity_on_source_evidence_blocks_coverage(self):
        case, legacy, current = setup_rows()
        current["reconstructionProof"]["sourceEvidence"][0]["multipleSeasons"] = True
        result = run_eval(case, legacy, current)
        self.assertEqual(result["currentCoverage"], "INCONCLUSIVE")

    def test_exact_current_gold_can_cover_when_historical_proof_missing(self):
        case, legacy, current = setup_rows()
        current["directUrlDiagnostic"].pop("reconstructionProof")
        result = run_eval(case, legacy, current)
        self.assertTrue(result["historicalExactMatch"])
        self.assertEqual(result["currentCoverage"], "COVERED")
        self.assertEqual(result["auditFidelityFinding"], "NONE")

    def test_proven_context_mismatch_survives_missing_historical_proof(self):
        case, legacy, current = setup_rows()
        current["directUrlDiagnostic"].pop("reconstructionProof")
        legacy.update(covered=False, primaryFailure="IDENTITY_REJECTED")
        current["identityContextComparison"] = {
            "different": True,
            "legacy": {"identityAccepted": False, "genericDiscovery": False},
            "productionEquivalent": {"identityAccepted": True, "genericDiscovery": True,
                                       "routeValid": True, "lifecycleParsed": True,
                                       "reconstructionPassed": True},
        }
        result = run_eval(case, legacy, current, mode="PRODUCTION_EQUIVALENT_CAPTURE")
        self.assertEqual(result["currentCoverage"], "COVERED")
        self.assertEqual(result["auditFidelityFinding"], "IDENTITY_CONTEXT_MISMATCH")

    def test_negative_case_never_counts_as_current_coverage(self):
        case, legacy, current = setup_rows()
        case["kind"] = "negative"
        result = run_eval(case, legacy, current)
        self.assertEqual(result["currentCoverage"], "NOT_COVERED")
        self.assertEqual(result["reasonCodes"], ["NON_POSITIVE_OR_UNREVIEWED_CASE"])

    def test_manual_review_case_never_counts_as_current_coverage(self):
        case, legacy, current = setup_rows()
        case["kind"] = "manual_review"
        case["goldReviewStatus"] = "MANUAL_REVIEW"
        result = run_eval(case, legacy, current)
        self.assertEqual(result["currentCoverage"], "NOT_COVERED")

    def test_same_season_status_change_is_conservatively_inconclusive(self):
        case, legacy, current = setup_rows(fact(), fact(status="FINAL_SEASON", announced="2025-04-01"))
        result = run_eval(case, legacy, current)
        self.assertEqual(result["temporalRelation"], "INCONCLUSIVE")

    def test_release_confirmed_summary_needs_lifecycle_and_bound_premiere_replay(self):
        value = fact(status="RELEASE_DATE_CONFIRMED", release_date="2027-05-12", release_year=2027)
        case, legacy, current = setup_rows(gold=value, current_fact=value)
        lifecycle = {"factType": "LIFECYCLE", "tmdbId": TMDB_ID, "title": TITLE,
                     "status": "RENEWED", "nextSeasonNumber": 2,
                     "sourceUrl": value["sourceUrl"], "sourceName": value["sourceName"],
                     "announcementDate": value["announcementDate"]}
        premiere = {"factType": "RELEASE_DATE", "tmdbId": TMDB_ID, "title": TITLE,
                    "status": "RELEASE_DATE_CONFIRMED", "nextSeasonNumber": 2,
                    "releaseDate": "2027-05-12", "releaseYear": 2027,
                    "sourceUrl": value["sourceUrl"], "sourceName": value["sourceName"],
                    "announcementDate": value["announcementDate"]}
        current["reconstructionProof"]["replayedFacts"] = [lifecycle, premiere]
        self.assertEqual(run_eval(case, legacy, current)["currentCoverage"], "COVERED")

    def test_release_confirmed_summary_without_lifecycle_binding_fails(self):
        value = fact(status="RELEASE_DATE_CONFIRMED", release_date="2027-05-12", release_year=2027)
        case, legacy, current = setup_rows(current_fact=value)
        current["reconstructionProof"]["replayedFacts"] = [{
            "factType": "RELEASE_DATE", "tmdbId": TMDB_ID, "title": TITLE,
            "status": "RELEASE_DATE_CONFIRMED", "nextSeasonNumber": 2,
            "releaseDate": "2027-05-12", "releaseYear": 2027,
            "sourceUrl": value["sourceUrl"], "sourceName": value["sourceName"],
            "announcementDate": value["announcementDate"]}]
        self.assertNotEqual(run_eval(case, legacy, current)["currentCoverage"], "COVERED")

    def test_missing_lifecycle_fact_is_not_coverage(self):
        self._reject_non_lifecycle_article("RELEASE_DATE")

    def _reject_non_lifecycle_article(self, fact_type):
        case, legacy, current = setup_rows(replay_type=fact_type)
        current_fact = fact(announced="2025-04-01")
        current["actualFacts"] = current_fact
        current["reconstructedFacts"] = copy.deepcopy(current_fact)
        current["reconstructionProof"] = proof(current_fact, replay_type=fact_type)
        result = run_eval(case, legacy, current)
        self.assertNotEqual(result["currentCoverage"], "COVERED")


if __name__ == "__main__":
    unittest.main()
