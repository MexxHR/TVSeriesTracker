"""Deterministic Phase 2A season/date/provenance regression cases."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import monitored as m
import update as u

TODAY = dt.date(2026, 9, 28)
ENTRY = {"tmdbId": 111110, "title": "ONE PIECE", "aliases": ["ONE PIECE"],
         "allowedDomains": ["netflix.com"]}
RENEWAL = "https://www.netflix.com/tudum/articles/one-piece-renewed-season-3"
PRODUCTION = "https://www.netflix.com/tudum/articles/one-piece-season-3-begins-production"
FIXTURES = Path(__file__).parent / "fixtures"


def detect(body, heading="ONE PIECE News", publication=None):
    article = {"title": heading, "body": body, "url": RENEWAL,
               "sourceName": "Netflix Tudum", "publicationDate": publication}
    return u.detect(m.scoped_article(article, ENTRY["aliases"]), ENTRY, TODAY,
                    extended_final=True, strict_binding=True)


class StrictBindingTests(unittest.TestCase):
    def test_multi_season_fixture_final_is_s3_without_s2_date(self):
        pages = {RENEWAL: (FIXTURES / "one_piece_multi_season_renewal.html").read_text(),
                 PRODUCTION: (FIXTURES / "one_piece_multi_season_production.html").read_text()}
        report = {"result": "candidate_found", "provider": "NETFLIX", "candidateUrls": list(pages),
                  "candidates": [{"candidateUrl": url, "factualParserEligible": True} for url in pages]}
        metadata = {"tmdbId": 111110, "title": "ONE PIECE", "networks": [{"name": "Netflix"}]}
        with tempfile.TemporaryDirectory() as tmp:
            result = m.process(111110, metadata=metadata, discoverer=lambda *_: report,
                               fetcher=lambda url, _: (pages[url], url), today=TODAY,
                               registry_path=Path(tmp) / "registry.json", audit_path=Path(tmp) / "audit.jsonl")
        self.assertEqual((result["pipelineState"], result["parsedSourceCount"]), ("VERIFIED_FACTS", 2))
        self.assertEqual((result["verifiedFacts"]["status"], result["verifiedFacts"]["nextSeasonNumber"]), ("RENEWED", 3))
        self.assertIsNone(result["verifiedFacts"]["releaseDate"])
        self.assertIsNone(result["verifiedFacts"]["releaseYear"])
        self.assertTrue(any(f["nextSeasonNumber"] == 2 and f["releaseDate"] == "2026-03-10" for f in result["sourceEvidence"]))
        self.assertFalse(any(f["nextSeasonNumber"] == 3 and f["rule"] in ("explicit-premiere", "explicit-release-year") for f in result["sourceEvidence"]))
        self.assertTrue(all(f.get("evidenceText") and len(f["evidenceText"]) <= 200 for f in result["sourceEvidence"]))

    def test_fixture_s2_date_has_distinct_publication_date(self):
        raw = (FIXTURES / "one_piece_multi_season_production.html").read_text()
        self.assertEqual(m.publication_date_from_html(raw), "2025-11-24")
        article = u.ADAPTERS["NETFLIX"].parse(raw, PRODUCTION)
        article["publicationDate"] = m.publication_date_from_html(raw)
        facts = u.detect(m.scoped_article(article, ENTRY["aliases"]), ENTRY, TODAY, strict_binding=True)
        dated = next(f for f in facts if f["releaseDate"])
        self.assertEqual((dated["nextSeasonNumber"], dated["releaseDate"], dated["announcementDate"]),
                         (2, "2026-03-10", "2025-11-24"))

    def test_s2_date_and_s3_renewal_in_one_sentence(self):
        facts = detect("ONE PIECE has been renewed for Season 3, ahead of the Season 2 premiere on March 10, 2026.")
        self.assertTrue(any(f["rule"] == "explicit-renewal" and f["nextSeasonNumber"] == 3 for f in facts))
        self.assertTrue(any(f["rule"] == "explicit-premiere" and f["nextSeasonNumber"] == 2 for f in facts))

    def test_s3_date_and_renewal_same_season(self):
        facts = detect("ONE PIECE has been renewed for Season 3. ONE PIECE Season 3 premieres August 2, 2027.")
        merged, _ = u.merge(None, facts, TODAY)
        self.assertEqual((merged["nextSeasonNumber"], merged["releaseDate"]), (3, "2027-08-02"))

    def test_s3_date_before_renewal_sentence(self):
        facts = detect("ONE PIECE Season 3 premieres August 2, 2027. ONE PIECE was renewed for Season 3.")
        merged, _ = u.merge(None, facts, TODAY)
        self.assertEqual(merged["releaseDate"], "2027-08-02")

    def test_s3_date_after_renewal_sentence(self):
        facts = detect("ONE PIECE was renewed for Season 3. ONE PIECE Season 3 premieres August 2, 2027.")
        merged, _ = u.merge(None, facts, TODAY)
        self.assertEqual(merged["releaseDate"], "2027-08-02")

    def test_two_season_dates_stay_separate(self):
        facts = detect("ONE PIECE Season 2 premieres March 10, 2026. ONE PIECE Season 3 premieres August 2, 2027.")
        self.assertEqual({(f["nextSeasonNumber"], f["releaseDate"]) for f in facts},
                         {(2, "2026-03-10"), (3, "2027-08-02")})

    def test_multiple_years_do_not_cross_bind(self):
        facts = detect("ONE PIECE Season 2 returns in 2026. ONE PIECE Season 3 returns in 2027.")
        self.assertEqual({(f["nextSeasonNumber"], f["releaseYear"]) for f in facts}, {(2, 2026), (3, 2027)})

    def test_publication_date_alone_is_not_release(self):
        facts = detect("ONE PIECE was renewed for Season 3.", publication="2026-03-10")
        self.assertEqual(facts[0]["announcementDate"], "2026-03-10")
        self.assertIsNone(facts[0]["releaseDate"])

    def test_body_date_without_structured_publication_is_not_announcement(self):
        facts = detect("ONE PIECE Season 2 premieres March 10, 2026.")
        self.assertIsNone(facts[0]["announcementDate"])

    def test_copyright_year_is_not_release_year(self):
        facts = detect("ONE PIECE renewed for Season 3. Copyright 2027 Netflix.")
        self.assertTrue(facts)
        self.assertTrue(all(f["releaseYear"] is None for f in facts))

    def test_unrelated_historical_year_is_not_release_year(self):
        facts = detect("ONE PIECE renewed for Season 3. ONE PIECE began in 2023 and gained fans in 2027.")
        self.assertTrue(all(f["releaseYear"] is None for f in facts))

    def test_explicit_s3_return_year_is_allowed(self):
        facts = detect("ONE PIECE Season 3 returns in 2027.")
        self.assertEqual((facts[0]["nextSeasonNumber"], facts[0]["releaseYear"], facts[0]["rule"]),
                         (3, 2027, "explicit-release-year"))
        self.assertEqual(facts[0]["factType"], "RELEASE_YEAR")

    def test_begins_production_is_not_premiere(self):
        self.assertEqual(detect("ONE PIECE Season 3 begins production in 2027."), [])

    def test_starts_filming_is_not_premiere(self):
        self.assertEqual(detect("ONE PIECE Season 3 starts filming March 10, 2026."), [])

    def test_now_in_production_is_not_premiere(self):
        self.assertEqual(detect("ONE PIECE Season 3 is now in production in 2027."), [])

    def test_filming_underway_is_not_premiere(self):
        self.assertEqual(detect("ONE PIECE Season 3 filming is underway in 2027."), [])

    def test_returns_to_production_is_not_release_year(self):
        self.assertEqual(detect("ONE PIECE Season 3 returns to production in 2027."), [])

    def test_production_article_with_old_premiere_only_dates_old_season(self):
        facts = detect("ONE PIECE Season 3 begins production. ONE PIECE Season 2 returns March 10, 2026.")
        self.assertEqual({f["nextSeasonNumber"] for f in facts}, {2})

    def test_renewal_provenance(self):
        fact = detect("ONE PIECE has been renewed for Season 3.")[0]
        self.assertEqual((fact["status"], fact["rule"]), ("RENEWED", "explicit-renewal"))
        self.assertEqual(fact["factType"], "LIFECYCLE")
        self.assertIn("renewed for Season 3", fact["evidenceText"])

    def test_direct_object_renewal_binds_exact_series_and_season(self):
        entry = {"tmdbId": 1, "title": "Anne Rice's Interview with the Vampire",
                 "aliases": ["Anne Rice's Interview with the Vampire", "Interview with the Vampire"]}
        raw = (FIXTURES / "amc_interview_direct_object_renewal.html").read_text(encoding="utf-8")
        article = u.ADAPTERS["NETFLIX"].parse(raw, "https://www.amcglobalmedia.com/example")
        facts = u.detect(article, entry, TODAY, strict_binding=True)
        self.assertEqual([(f["status"], f["nextSeasonNumber"], f["rule"]) for f in facts],
                         [("RENEWED", 4, "explicit-renewal")])

    def test_direct_object_renewal_rejects_other_series_and_split_sentences(self):
        entry = {"tmdbId": 1, "title": "Interview with the Vampire",
                 "aliases": ["Interview with the Vampire"]}
        for body in (
            "AMC renewed Mayfair Witches for a fourth season. Interview with the Vampire remains popular.",
            "AMC renewed Interview with the Vampire. A fourth season is planned.",
            "Interview with the Vampire appears in this article. AMC renewed Mayfair Witches for a fourth season.",
            "AMC has not renewed Interview with the Vampire for a fourth season.",
        ):
            with self.subTest(body=body):
                article = {"title": "AMC programming news", "body": body,
                           "url": "https://www.amcglobalmedia.com/example",
                           "sourceName": "AMC", "publicationDate": None}
                self.assertEqual(u.detect(article, entry, TODAY, strict_binding=True), [])

    def test_direct_object_renewal_does_not_join_unrelated_card(self):
        entry = {"tmdbId": 1, "title": "Interview with the Vampire",
                 "aliases": ["Interview with the Vampire"]}
        raw = ("<html><h1>Interview with the Vampire news</h1>"
               "<main><p>Interview with the Vampire cast update.</p></main>"
               "<aside><p>AMC renewed Mayfair Witches for a fourth season.</p></aside></html>")
        article = u.ADAPTERS["NETFLIX"].parse(raw, "https://www.amcglobalmedia.com/example")
        self.assertEqual(u.detect(article, entry, TODAY, strict_binding=True), [])

    def test_premiere_provenance(self):
        fact = detect("ONE PIECE Season 3 premieres August 2, 2027.")[0]
        self.assertEqual((fact["status"], fact["rule"]), ("RELEASE_DATE_CONFIRMED", "explicit-premiere"))
        self.assertEqual(fact["factType"], "RELEASE_DATE")
        self.assertIn("premieres August 2", fact["evidenceText"])

    def test_cancellation_provenance(self):
        fact = detect("ONE PIECE Season 3 was canceled.")[0]
        self.assertEqual((fact["status"], fact["rule"]), ("CANCELED", "explicit-cancellation"))

    def test_final_season_provenance(self):
        fact = detect("ONE PIECE Season 4 will be the final season.")[0]
        self.assertEqual((fact["status"], fact["rule"]), ("FINAL_SEASON", "explicit-final-season"))

    def test_evidence_fragment_bounded(self):
        fact = detect("ONE PIECE renewed for Season 3 " + "today " * 80)[0]
        self.assertLessEqual(len(fact["evidenceText"]), 200)

    def test_s3_renewal_s2_premiere_merge(self):
        facts = detect("ONE PIECE renewed for Season 3. ONE PIECE Season 2 premieres March 10, 2026.")
        merged, _ = u.merge(None, facts, TODAY)
        self.assertEqual((merged["nextSeasonNumber"], merged["releaseDate"], merged["releaseYear"]), (3, None, None))

    def test_s4_final_and_s4_premiere_merge(self):
        facts = detect("ONE PIECE Season 4 will be the final season. ONE PIECE Season 4 premieres July 9, 2027.")
        merged, _ = u.merge(None, facts, TODAY)
        self.assertEqual((merged["status"], merged["releaseDate"]), ("FINAL_SEASON", "2027-07-09"))

    def test_newer_season_precedence(self):
        facts = detect("ONE PIECE Season 3 premieres March 10, 2026. ONE PIECE renewed for Season 4.")
        merged, _ = u.merge(None, facts, TODAY)
        self.assertEqual((merged["nextSeasonNumber"], merged["releaseDate"]), (4, None))

    def test_same_season_conflicting_dates_fail(self):
        facts = detect("ONE PIECE Season 3 premieres August 2, 2027. ONE PIECE Season 3 premieres August 9, 2027.")
        with self.assertRaisesRegex(u.AutomationError, "Conflicting release dates"):
            u.merge(None, facts, TODAY)

    def test_url_slug_is_not_evidence(self):
        self.assertEqual(detect("ONE PIECE cast interviews."), [])

    def test_structured_publication_metadata_conflict_is_unknown(self):
        raw = '<meta property="article:published_time" content="2025-11-24"><time datetime="2026-03-10">Date</time>'
        self.assertIsNone(m.publication_date_from_html(raw))


if __name__ == "__main__":
    unittest.main()
