import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import monitored as m
import update as u


TODAY = dt.date(2026, 10, 10)
NOW = dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc)
AMC_DOMAIN = "amcglobalmedia.com"
DISNEY_DOMAIN = "press.disneyplus.com"

CASES = [
    ("AMC", "The Walking Dead: Daryl Dixon", AMC_DOMAIN,
     "https://www.amcglobalmedia.com/2025/07/28/the-walking-dead-daryl-dixon-renewed-for-a-fourth-and-final-season/",
     "The Walking Dead: Daryl Dixon Renewed for a Fourth and Final Season",
     "AMC has renewed The Walking Dead: Daryl Dixon for a fourth and final season.",
     "FINAL_SEASON", 4),
    ("AMC", "Anne Rice's Interview with the Vampire", AMC_DOMAIN,
     "https://www.amcglobalmedia.com/2026/08/01/interview-with-the-vampire-renewed/",
     "Anne Rice's Interview with the Vampire Renewed for Season 4",
     "AMC has renewed Anne Rice’s Interview with the Vampire for a fourth season.",
     "RENEWED", 4),
    ("AMC", "Mayfair Witches", AMC_DOMAIN,
     "https://www.amcglobalmedia.com/2026/08/02/mayfair-witches-season-4/",
     "Mayfair Witches Renewed for Season 4",
     "Mayfair Witches has been renewed for Season 4.",
     "RENEWED", 4),
    ("DISNEY_PLUS", "Percy Jackson and the Olympians", DISNEY_DOMAIN,
     "https://press.disneyplus.com/news/percy-jackson-and-the-olympians-season-2",
     "Percy Jackson and the Olympians Renewed for Season 2",
     "Percy Jackson and the Olympians has been renewed for season 2.",
     "RENEWED", 2),
    ("DISNEY_PLUS", "Wizards Beyond Waverly Place", DISNEY_DOMAIN,
     "https://press.disneyplus.com/news/wizards-beyond-waverly-place-season-2",
     "Wizards Beyond Waverly Place Renewed for Season 2",
     "Wizards Beyond Waverly Place has been renewed for season 2.",
     "RENEWED", 2),
    ("DISNEY_PLUS", "The Proud Family: Louder and Prouder", DISNEY_DOMAIN,
     "https://press.disneyplus.com/news/the-proud-family-louder-and-prouder-final-season",
     "The Proud Family: Louder and Prouder Final Season",
     "The Proud Family: Louder and Prouder Season 4 will be the final season.",
     "FINAL_SEASON", 4),
]


def provider_specs():
    return {
        "AMC": {"officialDomains": [AMC_DOMAIN], "candidateDomains": [AMC_DOMAIN], "seeds": ["https://www.amcglobalmedia.com/"]},
        "DISNEY_PLUS": {"officialDomains": [DISNEY_DOMAIN], "candidateDomains": [DISNEY_DOMAIN], "seeds": ["https://press.disneyplus.com/"]},
    }


def article_html(title, body, related=""):
    return ("<html><head><title>Official announcement</title></head><body>"
            f"<main><h1>{title}</h1><article><p>{body}</p></article></main>"
            f"<aside class='related-stories'><p>{related}</p></aside></body></html>")


class MonitoredV272Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.registry_path = Path(self.temp.name) / "monitored.json"
        self.audit_path = Path(self.temp.name) / "monitored.jsonl"
        self.mock_specs = patch.object(m.discovery, "provider_specs", side_effect=provider_specs)
        self.mock_specs.start()
        self.addCleanup(self.mock_specs.stop)

    def metadata(self, provider, title):
        network = "AMC" if provider == "AMC" else "Disney+"
        return {"tmdbId": 998877, "title": title, "networks": [{"name": network}]}

    def report(self, provider, url, *, eligible=True):
        candidate = {"candidateUrl": url, "factualParserEligible": eligible,
                     "evidenceLevel": "STRONG" if eligible else "SUPPORTED",
                     "provenance": "official_article_link"}
        return {"provider": provider, "result": "candidate_found", "routeSignals": ["networks:" + provider],
                "candidateUrls": [url], "candidates": [candidate]}

    def run_case(self, case, *, raw=None, url=None, dry=True):
        provider, title, domain, article_url, heading, body, _, _ = case
        meta = self.metadata(provider, title)
        report = self.report(provider, url or article_url)
        raw = raw or article_html(heading, body)
        return m.process(meta["tmdbId"], dry_run=dry, metadata=meta,
                         discoverer=lambda *_: report,
                         fetcher=lambda requested, domains: (raw, requested), today=TODAY, now=NOW,
                         registry_path=self.registry_path, audit_path=self.audit_path)

    def test_three_amc_and_three_disney_articles_parse_only_primary_editorial_facts(self):
        for case in CASES:
            with self.subTest(provider=case[0], title=case[1]):
                unrelated = "Unrelated Series Season 99 was renewed for Season 99."
                result = self.run_case(case, raw=article_html(case[4], case[5], unrelated))
                self.assertEqual(result["pipelineState"], "VERIFIED_FACTS", result)
                self.assertEqual(result["verifiedFacts"]["status"], case[6])
                self.assertEqual(result["verifiedFacts"]["nextSeasonNumber"], case[7])
                self.assertEqual(result["sourceEvidence"][0]["sourceUrl"], case[3])
                self.assertIsNone(result["verifiedFacts"]["releaseDate"])
                self.assertEqual(result["routeSignals"], ["networks:" + case[0]])
                self.assertTrue(result["sourceChecks"][0]["headlineBoundToMetadataTitle"])
                self.assertFalse(self.registry_path.exists())
                self.assertFalse(self.audit_path.exists())

    def test_no_explicit_evidence_does_not_invent_facts(self):
        case = CASES[0]
        result = self.run_case(case, raw=article_html("The Walking Dead: Daryl Dixon Season 4 News",
                                                       "The cast discussed the series at a fan event."))
        self.assertEqual(result["pipelineState"], "NO_VERIFIED_FACTS")
        self.assertEqual(result["verifiedFacts"], {})
        self.assertEqual(result["sourceEvidence"], [])

    def test_offdomain_candidate_is_rejected_before_fetch(self):
        case = CASES[3]
        result = self.run_case(case, url="https://press.disneyplus.com.evil.example/news/percy-jackson",
                               raw=article_html(case[4], case[5]))
        self.assertEqual(result["pipelineState"], "VALIDATION_FAILED")
        self.assertFalse(result["sourceChecks"][0]["candidateDomainAllowed"])
        self.assertEqual(result["verifiedFacts"], {})

    def test_wrong_headline_identity_is_rejected(self):
        case = CASES[3]
        result = self.run_case(case, raw=article_html("A Different Series Renewed for Season 2", case[5]))
        self.assertEqual(result["pipelineState"], "VALIDATION_FAILED")
        self.assertFalse(result["sourceChecks"][0]["headlineBoundToMetadataTitle"])
        self.assertEqual(result["verifiedFacts"], {})

    def test_publisher_leading_qualified_headlines_survive_second_fetch(self):
        examples = (
            (CASES[1], "AMC Global Media Renews Anne Rice’s Interview with the Vampire for a Fourth Season"),
            (CASES[3], "Disney+ Renews Percy Jackson and the Olympians for Season 2"),
        )
        for case, heading in examples:
            with self.subTest(heading=heading):
                result = self.run_case(case, raw=article_html(heading, case[5]))
                self.assertEqual(result["pipelineState"], "VERIFIED_FACTS", result)
                self.assertTrue(result["sourceChecks"][0]["headlineBoundToMetadataTitle"])

    def test_wrong_season_date_is_not_borrowed_for_a_renewal(self):
        case = CASES[3]
        body = ("Percy Jackson and the Olympians was renewed for season 3. "
                "Percy Jackson and the Olympians Season 2 premieres March 10, 2027.")
        result = self.run_case(case, raw=article_html("Percy Jackson and the Olympians Renewed for Season 3", body))
        self.assertEqual(result["pipelineState"], "VERIFIED_FACTS", result)
        self.assertEqual(result["verifiedFacts"]["status"], "RENEWED")
        self.assertEqual(result["verifiedFacts"]["nextSeasonNumber"], 3)
        self.assertIsNone(result["verifiedFacts"]["releaseDate"])
        season_three_lifecycle = [row for row in result["sourceEvidence"]
                                  if row["factType"] == "LIFECYCLE"]
        self.assertTrue(season_three_lifecycle)
        self.assertTrue(all(row["nextSeasonNumber"] == 3 and row["releaseDate"] is None
                            for row in season_three_lifecycle))

    def test_unrelated_editorial_text_does_not_create_a_fact(self):
        case = CASES[4]
        result = self.run_case(case, raw=article_html("Wizards Beyond Waverly Place Season 2",
                                                       "Another Show was renewed for season 2."))
        self.assertEqual(result["pipelineState"], "NO_VERIFIED_FACTS")
        self.assertEqual(result["verifiedFacts"], {})

    def test_unrelated_final_season_and_navigation_do_not_contaminate_target(self):
        case = CASES[3]
        raw = article_html("Percy Jackson and the Olympians Season 2 News",
                           "Percy Jackson and the Olympians cast joined the event. "
                           "Another Series season 4 will be the final season.",
                           "Percy Jackson and the Olympians season 9 will be the final season.")
        result = self.run_case(case, raw=raw)
        self.assertEqual(result["pipelineState"], "NO_VERIFIED_FACTS", result)
        self.assertEqual(result["verifiedFacts"], {})

    def test_dry_run_proposal_and_idempotent_no_write(self):
        production = [u.DATA, u.REGISTRY, u.AUDIT]
        production_before = [path.read_bytes() if path.exists() else None for path in production]
        case = CASES[2]
        proposal = self.run_case(case)
        self.assertEqual((proposal["pipelineState"], proposal["registryChange"]), ("VERIFIED_FACTS", "would_add"))
        self.assertEqual(proposal["proposedRecord"]["verifiedFacts"]["nextSeasonNumber"], 4)
        self.assertFalse(self.registry_path.exists())
        self.assertFalse(self.audit_path.exists())

        self.registry_path.write_text(json.dumps({"schemaVersion": 1,
                                                  "series": [proposal["proposedRecord"]]}), encoding="utf-8")
        before = self.registry_path.read_bytes()
        no_op = self.run_case(case)
        self.assertEqual((no_op["pipelineState"], no_op["registryChange"]), ("VERIFIED_FACTS", "none"))
        self.assertEqual(before, self.registry_path.read_bytes())
        self.assertFalse(self.audit_path.exists())
        self.assertEqual(production_before,
                         [path.read_bytes() if path.exists() else None for path in production])


if __name__ == "__main__":
    unittest.main()
