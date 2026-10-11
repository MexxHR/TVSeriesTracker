"""Regression coverage for title-bound Hulu Shrill evidence replay."""
from pathlib import Path
import sys
import datetime as dt
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import fx_hulu_qualification as qualification
import promotion
import update


SHRILL_URL = (
    "https://press.hulu.com/pressrelease/"
    "hulu-presents-upcoming-original-series-the-handmaids-tale-season-four-"
    "the-final-season-of-shrill-and-marvels-m-o-d-o-k-at-2021-television-"
    "critics-association-winter-press-tour/"
)
SHRILL_HTML = """<html><head>
<title>Hulu Presents Upcoming Original Series The Handmaid's Tale Season Four, the Final Season of Shrill and Marvel's M.O.D.O.K.</title>
<meta property="article:published_time" content="2021-02-24T09:00:00-08:00">
</head><body><h1>Hulu Presents Upcoming Original Series The Handmaid's Tale Season Four, the Final Season of Shrill and Marvel's M.O.D.O.K.</h1>
<article>
<p>Today, during the Television Critics Association's Winter Press Tour, Hulu presented the company's lineup of upcoming original programming including the fourth season of Emmy, Golden Globe and Peabody Award-winning series "The Handmaid's Tale," the third and final season of comedy series "Shrill" starring Aidy Bryant, and new adult animation series "Marvel's M.O.D.O.K.," all of which join the company's collection of premium original content.</p>
<p>The company also announced an early third season renewal for “Animaniacs” from Steven Spielberg.</p>
<p>The third and final season of "Shrill" premieres all eight episodes on Friday, May 7. The new season finds Annie energized by her breakup and her newfound momentum at work.</p>
</article></body></html>"""


class ShrillReconstructionTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = lambda url, domains: (
            {"status": "fetched", "finalUrl": SHRILL_URL}, SHRILL_HTML
        )
        self.case = {"series": "Shrill", "aliases": ["Shrill"],
                     "discoveredUrl": SHRILL_URL, "discoveredFinalUrl": SHRILL_URL}

    def test_multi_title_hulu_release_reconstructs_shrill_final_season(self):
        result = qualification._fact_reconstruction(
            self.case, 1, ["press.hulu.com"], dt.date(2026, 10, 11), self.fetcher
        )

        self.assertEqual(result["result"], "passed")
        self.assertEqual(result["verifiedFacts"]["status"], "FINAL_SEASON")
        self.assertEqual(result["verifiedFacts"]["nextSeasonNumber"], 3)
        self.assertEqual(result["verifiedFacts"]["releaseDate"], "2021-05-07")
        self.assertEqual({item["factType"] for item in result["sourceEvidence"]},
                         {"LIFECYCLE", "RELEASE_DATE"})
        for evidence in result["sourceEvidence"]:
            self.assertIn("Shrill", evidence["evidenceText"])
            self.assertLessEqual(len(evidence["evidenceText"]), 200)
            self.assertEqual(evidence["nextSeasonNumber"], 3)
        self.assertTrue(all(item["sourceUrl"] == SHRILL_URL for item in result["sourceEvidence"]))

    def test_unrelated_titles_do_not_supply_a_shrill_season_claim(self):
        raw = """<html><head><title>Hulu release slate</title></head><body>
<h1>Hulu release slate</h1><article>
<p>The fourth and final season of The Handmaid's Tale premieres in April.</p>
<p>Shrill joins Hulu's comedy lineup this year.</p>
</article></body></html>"""
        fetcher = lambda url, domains: ({"status": "fetched", "finalUrl": SHRILL_URL}, raw)
        result = qualification._fact_reconstruction(
            self.case, 1, ["press.hulu.com"], dt.date(2026, 10, 11), fetcher
        )

        self.assertEqual(result["result"], "not_applicable")
        self.assertEqual(result["reason"], "strict_parser_returned_no_facts")

    def test_separate_lifecycle_and_date_claims_keep_distinct_replayable_excerpts(self):
        filler = " Hulu described the broader slate and production plans for its returning original series " * 3
        raw = f"""<html><head><title>Shrill season announcement</title>
<meta property="article:published_time" content="2021-02-24"></head><body>
<h1>Shrill season announcement</h1><article><p>The third and final season of Shrill,{filler} and the third season of Shrill premieres on Friday, May 7.</p>
</article></body></html>"""
        fetcher = lambda url, domains: ({"status": "fetched", "finalUrl": SHRILL_URL}, raw)
        result = qualification._fact_reconstruction(
            self.case, 1, ["press.hulu.com"], dt.date(2026, 10, 11), fetcher
        )

        self.assertEqual(result["result"], "passed")
        excerpts = {item["factType"]: item["evidenceText"] for item in result["sourceEvidence"]}
        self.assertIn("final season", excerpts["LIFECYCLE"])
        self.assertIn("premieres", excerpts["RELEASE_DATE"])
        self.assertNotEqual(excerpts["LIFECYCLE"], excerpts["RELEASE_DATE"])

    def test_negated_shrill_claim_is_not_accepted_beside_another_title(self):
        raw = """<html><head><title>Hulu release slate</title></head><body>
<h1>Hulu release slate</h1><article>
<p>The fourth season of The Handmaid's Tale is discussed. It is false that Hulu renewed Shrill for a fourth season.</p>
</article></body></html>"""
        fetcher = lambda url, domains: ({"status": "fetched", "finalUrl": SHRILL_URL}, raw)
        result = qualification._fact_reconstruction(
            self.case, 1, ["press.hulu.com"], dt.date(2026, 10, 11), fetcher
        )

        self.assertEqual(result["result"], "not_applicable")
        self.assertEqual(result["reason"], "strict_parser_returned_no_facts")

    def test_other_show_lifecycle_claims_cannot_borrow_shrill_identity(self):
        bodies = [
            "The third and final season of Other Show premieres in May, while Shrill continues its second season.",
            "Shrill continues its second season, while Other Show's third and final season premieres in May.",
            "Other Show has been renewed for a third season, while Shrill continues its second season.",
            "Shrill continues its second season, while Other Show has been renewed for a third season.",
            "Shrill is in a new comedy and Other Show has been renewed for a third season.",
            "Shrill continues its second season and Other Show has been renewed for a third season.",
            "Shrill joins Other Show for its third and final season.",
            "Other Show's third season was canceled, while Shrill continues its second season.",
        ]
        for index, body in enumerate(bodies):
            with self.subTest(case=index):
                raw = f"<html><head><title>Hulu slate</title></head><body><h1>Hulu slate</h1><article><p>{body}</p></article></body></html>"
                fetcher = lambda url, domains, page=raw: ({"status": "fetched", "finalUrl": SHRILL_URL}, page)
                result = qualification._fact_reconstruction(
                    self.case, 1, ["press.hulu.com"], dt.date(2026, 10, 11), fetcher
                )
                self.assertEqual(result["result"], "not_applicable")
                self.assertEqual(result["reason"], "strict_parser_returned_no_facts")

    def test_direct_renewal_headlines_and_daryl_final_season_phrases_parse(self):
        cases = [
            ("Running Point", "Running Point Gets Renewed for Season 3 Kate Hudson and Justin Theroux face off again"),
            ("The Walking Dead: Daryl Dixon", "The Walking Dead: Daryl Dixon Renewed for a Fourth and Final Season"),
            ("The Walking Dead: Daryl Dixon", "AMC has renewed The Walking Dead: Daryl Dixon for a fourth and final season."),
        ]
        for title, heading in cases:
            with self.subTest(title=title, heading=heading):
                facts = update.detect(
                    {"title": heading, "body": "", "publicationDate": None,
                     "sourceName": "official", "url": "https://example.test/article"},
                    {"tmdbId": 1, "title": title, "aliases": [title]},
                    dt.date(2026, 10, 11), strict_binding=True)
                self.assertTrue(facts, heading)
                self.assertEqual(facts[0]["nextSeasonNumber"], 3 if title == "Running Point" else 4)

    def test_failed_replay_reports_safe_error_stage_and_available_evidence(self):
        with patch.object(promotion, "_validate_evidence",
                          side_effect=update.AutomationError(
                              "Persisted fact does not match strict parser evidence")):
            result = qualification._fact_reconstruction(
                self.case, 1, ["press.hulu.com"], dt.date(2026, 10, 11), self.fetcher
            )

        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["reason"], "AutomationError")
        self.assertEqual(result["detail"], "Persisted fact does not match strict parser evidence")
        self.assertEqual(result["failureStage"], "independent_evidence_replay")
        self.assertEqual(result["sourceEvidence"][0]["factType"], "LIFECYCLE")
        self.assertIn("Shrill", result["sourceEvidence"][0]["evidenceText"])
        self.assertNotIn("<article>", str(result))
        self.assertNotIn("authorization", str(result).casefold())


if __name__ == "__main__":
    unittest.main()
