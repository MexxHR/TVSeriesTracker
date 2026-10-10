"""Regression coverage for generic, season-bound production announcements."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parent))
import discovery
import monitored
import promotion
import update


TODAY = dt.date(2026, 9, 28)
ARTICLE_URL = ("https://www.amcglobalmedia.com/2026/06/22/"
               "amc-global-medias-critically-acclaimed-drama-the-audacity-"
               "commences-season-2-production/")
TITLE = "AMC Global Media’s Critically Acclaimed Drama, The Audacity, Commences Season 2 Production"
LEAD = ("NEW YORK – June 18, 2026 – AMC Global Media today announced that production of the much "
        "anticipated eight-episode second season of The Audacity is underway in Vancouver, Canada, "
        "with the series returning to AMC and AMC+ in 2027.")
ENTRY = {"tmdbId": 258036, "title": "The Audacity", "aliases": ["The Audacity"],
         "allowedDomains": ["amcglobalmedia.com"]}


def article(title=TITLE, body=LEAD):
    return {"title": title, "body": body, "url": ARTICLE_URL, "sourceName": "AMC Networks",
            "publicationDate": "2026-06-22"}


class AudacityProductionParserTests(unittest.TestCase):
    def test_official_article_emits_season_lifecycle_and_bound_year_without_date(self):
        facts = update.detect(article(), ENTRY, TODAY, strict_binding=True)
        lifecycle = next(f for f in facts if f["factType"] == "LIFECYCLE")
        year = next(f for f in facts if f["factType"] == "RELEASE_YEAR")
        self.assertEqual((lifecycle["status"], lifecycle["nextSeasonNumber"], lifecycle["rule"]),
                         ("RENEWED", 2, "explicit-production-start"))
        self.assertEqual(lifecycle["announcementDate"], "2026-06-22")
        self.assertEqual((year["nextSeasonNumber"], year["releaseYear"], year["releaseDate"]), (2, 2027, None))
        merged, _ = update.merge(None, facts, TODAY)
        self.assertEqual((merged["status"], merged["nextSeasonNumber"], merged["releaseYear"], merged["releaseDate"]),
                         ("RENEWED", 2, 2027, None))

    def test_production_fact_is_strict_binding_only_and_excerpt_replays(self):
        facts = update.detect(article(), ENTRY, TODAY, strict_binding=True)
        self.assertEqual(update.detect(article(), ENTRY, TODAY), [])
        self.assertTrue(all(0 < len(f["evidenceText"]) <= 200 for f in facts))
        self.assertTrue(all("The Audacity" in f["evidenceText"] and "Season" in f["evidenceText"]
                            or "second season" in f["evidenceText"].lower() for f in facts))
        year = next(f for f in facts if f["factType"] == "RELEASE_YEAR")
        self.assertIn("in 2027", year["evidenceText"])
        row = {"tmdbId": ENTRY["tmdbId"], "title": ENTRY["title"], "lastChecked": TODAY.isoformat(),
               "parserResult": "facts", "validationResult": "passed",
               "eligibleSourceUrls": [ARTICLE_URL], "sourceEvidence": [
                   {**{key: fact.get(key) for key in monitored.EVIDENCE_FIELDS},
                    "publicationDateSource": "structured_metadata"}
                   for fact in facts]}
        replayed = promotion._validate_evidence(row, ENTRY["allowedDomains"])
        self.assertEqual({(f["factType"], f["nextSeasonNumber"], f["releaseYear"]) for f in replayed},
                         {("LIFECYCLE", 2, None), ("RELEASE_YEAR", 2, 2027)})

    def test_long_existing_premiere_year_keeps_title_in_evidence_excerpt(self):
        body = ("The Audacity Season 2 production has wrapped and premieres in 2027 with "
                + "a wonderful cast and crew working together " * 3 + "for everyone")
        facts = update.detect(article(title="AMC programming update", body=body), ENTRY, TODAY,
                              strict_binding=True)
        year = next(f for f in facts if f["factType"] == "RELEASE_YEAR")
        self.assertIn("The Audacity Season 2", year["evidenceText"])
        replayed = update.detect(article(title="", body=year["evidenceText"]), ENTRY, TODAY,
                                 strict_binding=True)
        self.assertTrue(any(f["factType"] == "RELEASE_YEAR" and f["releaseYear"] == 2027
                            for f in replayed))

    def test_commences_season_production_headline_is_explicit(self):
        facts = update.detect(article(body="AMC announced a cast update for The Audacity."),
                              ENTRY, TODAY, strict_binding=True)
        self.assertTrue(any(f["factType"] == "LIFECYCLE" and f["nextSeasonNumber"] == 2 for f in facts))

    def test_ten_required_adversarial_examples_emit_no_lifecycle(self):
        cases = (
            "The Audacity production continues.",
            "The Audacity production team discusses Season 2.",
            "The Audacity Season 2 production designer joins the series.",
            "Production begins on Another Show Season 2. The Audacity cast celebrates.",
            "The Audacity star previously began production on Other Show Season 2.",
            "The Audacity Season 1 began production in 2025.",
            "The Audacity is produced by AMC Studios.",
            "Season 2 production is underway.",
            "The Audacity begins Season 2 production while Better Call Saul Season 3 is underway.",
            "The Audacity cast profile: Billy Magnussen began production on Season 2 of Other Show.",
        )
        for text in cases:
            with self.subTest(text=text):
                facts = update.detect(article(title="AMC programming update", body=text), ENTRY, TODAY,
                                      strict_binding=True)
                self.assertFalse(any(f["factType"] == "LIFECYCLE" for f in facts))

    def test_negation_and_uncertainty_do_not_establish_production(self):
        cases = (
            "The Audacity Season 2 has not begun production.",
            "Production on The Audacity Season 2 is expected to begin next year.",
            "The Audacity Season 2 may begin production soon.",
            "The Audacity Season 2 production is rumored to be underway.",
        )
        for text in cases:
            with self.subTest(text=text):
                facts = update.detect(article(title="AMC programming update", body=text), ENTRY, TODAY,
                                      strict_binding=True)
                self.assertFalse(any(f["factType"] == "LIFECYCLE" for f in facts))

    def test_production_roles_discussion_and_hedging_do_not_establish_lifecycle(self):
        cases = (
            "The Audacity Season 2 production designer started a new job.",
            "The Audacity Season 2 production team has begun discussing locations.",
            "The Audacity Season 2 production is believed to be underway.",
            "The Audacity Season 2 production team met while Another Show is underway.",
        )
        for text in cases:
            with self.subTest(text=text):
                facts = update.detect(article(title="AMC programming update", body=text), ENTRY, TODAY,
                                      strict_binding=True)
                self.assertFalse(any(f["factType"] == "LIFECYCLE" for f in facts))

    def test_return_year_requires_affirmative_series_bound_subject(self):
        negated = update.detect(article(title="AMC programming update",
                                        body="The Audacity Season 2 is not returning to AMC in 2027."),
                                ENTRY, TODAY, strict_binding=True)
        self.assertFalse(any(f["factType"] in ("RELEASE_DATE", "RELEASE_YEAR") for f in negated))
        another_series = update.detect(
            article(title="AMC programming update",
                    body="The Audacity Season 2 production is underway, with Another Show returning to AMC in 2027."),
            ENTRY, TODAY, strict_binding=True)
        self.assertFalse(any(f["factType"] in ("RELEASE_DATE", "RELEASE_YEAR") for f in another_series))
        competing_names = update.detect(
            article(title="AMC programming update",
                    body="The Audacity Season 2 and Another Show return to AMC in 2027."),
            ENTRY, TODAY, strict_binding=True)
        self.assertFalse(any(f["factType"] in ("RELEASE_DATE", "RELEASE_YEAR") for f in competing_names))

    def test_modal_or_uncertain_return_does_not_establish_release_year(self):
        cases = (
            "The Audacity Season 2 may be returning to AMC in 2027.",
            "The Audacity Season 2 production is underway, with the series potentially returning to AMC in 2027.",
        )
        for text in cases:
            with self.subTest(text=text):
                facts = update.detect(article(title="AMC programming update", body=text), ENTRY, TODAY,
                                      strict_binding=True)
                self.assertFalse(any(f["factType"] in ("RELEASE_DATE", "RELEASE_YEAR") for f in facts))

    def test_existing_amc_and_disney_renewal_patterns_remain_accepted(self):
        examples = (
            ({"tmdbId": 1, "title": "Interview with the Vampire",
              "aliases": ["Interview with the Vampire"]},
             "AMC renewed Interview with the Vampire for a fourth season.", 4),
            ({"tmdbId": 2, "title": "Wizards Beyond Waverly Place",
              "aliases": ["Wizards Beyond Waverly Place"]},
             "Wizards Beyond Waverly Place has been renewed for Season 2.", 2),
        )
        for entry, text, season in examples:
            with self.subTest(title=entry["title"]):
                found = update.detect({**article(title="Official announcement", body=text),
                                       "url": "https://www.amcglobalmedia.com/example"},
                                      entry, TODAY, strict_binding=True)
                self.assertTrue(any(f["factType"] == "LIFECYCLE" and f["nextSeasonNumber"] == season
                                    for f in found))


class AudacityMonitoredDryRunTests(unittest.TestCase):
    def test_real_amc_discovery_and_monitored_process_is_read_only(self):
        homepage = discovery.AMC_SEARCH_HOME
        search = homepage + "?" + urlencode({"s": "The Audacity"})
        raw_article = (f"<html><head><meta property='article:published_time' content='2026-06-22'>"
                       f"<title>{TITLE}</title></head><body><h1>{TITLE}</h1><main><p>{LEAD}</p>"
                       "<p>The Audacity is an AMC Studios production with worldwide rights held by AMC Global Media.</p>"
                       "</main></body></html>")
        pages = {
            homepage: '<form method="get" action="/"><input name="s" type="search"></form>',
            search: f'<h1>Search results</h1><a href="{ARTICLE_URL}">The Audacity Season 2 Production</a>',
            ARTICLE_URL: raw_article,
        }

        def fixture_fetch(url, domains):
            if url not in pages:
                return "<html><title>News</title><h1>News</h1></html>", url
            return pages[url], url

        existing = {"tmdbId": ENTRY["tmdbId"], "title": ENTRY["title"], "provider": "AMC",
                    "discoveryState": "NO_VERIFIED_FACTS", "candidateUrls": [], "verifiedFacts": {},
                    "sourceEvidence": [], "lastChecked": "2026-09-01"}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            registry_path = Path(tmp) / "monitored.json"
            audit_path = Path(tmp) / "audit.jsonl"
            registry_path.write_text(json.dumps({"schemaVersion": 1, "series": [existing]}), encoding="utf-8")
            trusted_files = [update.DATA, update.REGISTRY, update.AUDIT,
                             monitored.MONITORED, monitored.MONITORED_AUDIT]
            before = [(path.read_bytes() if path.exists() else None) for path in trusted_files]
            result = monitored.process(
                ENTRY["tmdbId"], dry_run=True,
                metadata={"tmdbId": ENTRY["tmdbId"], "title": "The Audacity", "networks": [{"name": "AMC"}]},
                discoverer=lambda meta, trusted: discovery.discover(meta, trusted, fetcher=fixture_fetch),
                fetcher=fixture_fetch, today=TODAY, registry_path=registry_path, audit_path=audit_path)
            self.assertEqual((result["pipelineState"], result["registryChange"]), ("VERIFIED_FACTS", "would_update"))
            self.assertEqual((result["verifiedFacts"]["status"], result["verifiedFacts"]["nextSeasonNumber"],
                              result["verifiedFacts"]["releaseYear"], result["verifiedFacts"]["releaseDate"]),
                             ("RENEWED", 2, 2027, None))
            replayed = promotion._validate_evidence(result["proposedRecord"],
                                                     discovery.provider_specs()["AMC"]["officialDomains"])
            self.assertTrue(any(f.get("releaseYear") == 2027 for f in replayed))
            self.assertEqual(before, [(path.read_bytes() if path.exists() else None) for path in trusted_files])
            self.assertFalse(audit_path.exists())


if __name__ == "__main__":
    unittest.main()
