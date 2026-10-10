"""Focused strict-parser regressions for the FX/Hulu qualification gaps."""
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import monitored
import promotion
import update


TODAY = dt.date(2026, 10, 10)
HULU_URL = "https://press.hulu.com/pressrelease/hulu-renews-difficult-people-for-season-two/"
HULU_ENTRY = {"tmdbId": 123, "title": "Difficult People", "aliases": ["Difficult People"]}
FX_ENTRY = {"tmdbId": 456, "title": "Mayans M.C.", "aliases": ["Mayans M.C.", "Mayans"]}


def article(title, body, url=HULU_URL, source="Hulu Press"):
    return {"title": title, "body": body, "url": url, "sourceName": source,
            "publicationDate": "2015-08-27"}


class HuluPickedUpParserTests(unittest.TestCase):
    def test_official_difficult_people_pickup_wording_is_a_bound_renewal(self):
        found = update.detect(article(
            "Hulu Renews Difficult People for Season Two",
            "Hulu Original Difficult People has been picked up for a second season."),
            HULU_ENTRY, TODAY, strict_binding=True)
        self.assertEqual(len(found), 1)
        fact = found[0]
        self.assertEqual((fact["factType"], fact["status"], fact["nextSeasonNumber"], fact["rule"]),
                         ("LIFECYCLE", "RENEWED", 2, "explicit-renewal"))
        self.assertEqual(fact["evidenceText"],
                         "Hulu Original Difficult People has been picked up for a second season")
        replay = update.detect(article("", fact["evidenceText"]), HULU_ENTRY, TODAY,
                               strict_binding=True)
        self.assertEqual([(f["factType"], f["status"], f["nextSeasonNumber"], f["rule"]) for f in replay],
                         [("LIFECYCLE", "RENEWED", 2, "explicit-renewal")])

        row = {"tmdbId": HULU_ENTRY["tmdbId"], "title": HULU_ENTRY["title"],
               "lastChecked": TODAY.isoformat(), "parserResult": "facts",
               "validationResult": "passed", "eligibleSourceUrls": [HULU_URL],
               "sourceEvidence": [{**{key: fact.get(key) for key in monitored.EVIDENCE_FIELDS},
                                   "publicationDateSource": "structured_metadata"}],
               "verifiedFacts": {key: fact.get(key) for key in monitored.FACT_FIELDS}}
        independent = promotion._validate_evidence(row, ["press.hulu.com"])
        summarized = promotion._facts_summary(independent, row, TODAY)
        self.assertEqual({key: summarized.get(key) for key in monitored.FACT_FIELDS},
                         {key: fact.get(key) for key in monitored.FACT_FIELDS})

    def test_pickup_syntax_supports_explicit_season_wording_generically(self):
        for wording in ("Difficult People has been picked up for a third season.",
                        "Difficult People was picked up for Season 3.",
                        "Hulu announced today that Difficult People has been picked up for a third season."):
            with self.subTest(wording=wording):
                found = update.detect(article("Hulu programming news", wording), HULU_ENTRY, TODAY,
                                      strict_binding=True)
                self.assertTrue(any(f["status"] == "RENEWED" and f["nextSeasonNumber"] == 3
                                    for f in found))

    def test_pickup_rule_rejects_identity_and_structure_ambiguity(self):
        cases = (
            # Identity missing from the factual sentence.
            "A comedy series has been picked up for a second season.",
            # The requested series is discussed, but another series is picked up.
            "Difficult People remains popular, while Another Series has been picked up for a second season.",
            # Same sentence, but title is not the grammatical subject of pickup.
            "Difficult People star profiles discuss how Another Series has been picked up for a second season.",
            # The title and lifecycle assertion occur in separate sentences.
            "Difficult People is a Hulu Original. Another Series has been picked up for a second season.",
            # A biography mentioning another show's pickup is not evidence.
            "Difficult People actor Jane Doe previously starred in Another Series, which was picked up for a second season.",
            # Pickup without a numbered season cannot produce a numbered renewal.
            "Difficult People has been picked up for more episodes.",
            # Ambiguous marketing language has no explicit season decision.
            "Hulu picked up Difficult People for its comedy collection.",
            # Negated or speculative lifecycle wording is not affirmative.
            "Difficult People has not been picked up for a second season.",
            "It is false that Difficult People has been picked up for a second season.",
            "Rumors that Difficult People has been picked up for a second season are untrue.",
            "It was rumored that Difficult People has been picked up for a second season.",
            "Difficult People has been picked up for a second season, according to unconfirmed reports.",
            "Hulu denied that Difficult People has been picked up for a second season.",
        )
        for body in cases:
            with self.subTest(body=body):
                found = update.detect(article("Hulu programming news", body), HULU_ENTRY, TODAY,
                                      strict_binding=True)
                self.assertFalse(any(f["factType"] == "LIFECYCLE" for f in found))


class FxMayansEvidenceBoundaryTests(unittest.TestCase):
    def test_separate_final_season_and_season_five_headings_remain_unbound(self):
        found = update.detect(article("Mayans M.C. | FX", "FINAL SEASON\nSeason 5",
                                      "https://www.fxnetworks.com/shows/mayans-mc", "FX"),
                              FX_ENTRY, TODAY, strict_binding=True)
        self.assertEqual(found, [])

    def test_mayans_page_and_article_style_fragments_do_not_infer_finality(self):
        cases = (
            "Mayans M.C. Season 5 premieres this summer. Watch the final trailer.",
            "Mayans M.C. Season 5: the final episodes arrive Friday.",
            "Mayans M.C. Season 5 reaches its finale this week.",
            "Mayans M.C. Season 5 follows the final events from Season 4.",
            "Mayans M.C. returns for Season 5.",
            "Mayans M.C. appears in this story. Another FX series' final season begins soon.",
            "Mayans M.C. cast profile: actor Jane Doe appeared in Another Show's final season.",
            "The FX series Season 5 is its final season.",
            "Mayans M.C. is entering a new chapter.",
        )
        for body in cases:
            with self.subTest(body=body):
                found = update.detect(article("FX programming news", body,
                                              "https://www.fxnetworks.com/shows/mayans-mc", "FX"),
                                      FX_ENTRY, TODAY, strict_binding=True)
                self.assertFalse(any(f["status"] == "FINAL_SEASON" for f in found))


if __name__ == "__main__":
    unittest.main()
