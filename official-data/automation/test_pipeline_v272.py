"""Offline production-path dry runs for the two newly enabled providers."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import discovery
import monitored
import promotion
import update


class ProductionAdapterDryRunTests(unittest.TestCase):
    def test_actual_discovery_monitored_parser_and_promotion_rebuild(self):
        cases = (
            ("AMC", 990901, "Dark Winds", "AMC",
             "https://www.amcglobalmedia.com/2026/02/05/amcs-critically-acclaimed-drama-dark-winds-renewed-for-a-fifth-season-ahead-of-the-season-4-premiere/",
             "AMC's Critically Acclaimed Drama Dark Winds Renewed for a Fifth Season",
             "AMC has renewed Dark Winds for a fifth season. The acclaimed television series will return with its cast and creative team.",
             "RENEWED", 5),
            ("DISNEY_PLUS", 990902, "Percy Jackson and the Olympians", "Disney+",
             "https://press.disneyplus.com/news/disney-plus-percy-jackson-and-the-olympians-season-two-announcement",
             "Disney+ Renews Percy Jackson and the Olympians for Season Two",
             "Disney+ has renewed Percy Jackson and the Olympians for season two. The series will return with its cast and creative team.",
             "RENEWED", 2),
        )
        protected = (update.DATA, update.AUDIT, update.REGISTRY, monitored.MONITORED,
                     monitored.MONITORED_AUDIT)
        before = [path.read_bytes() for path in protected]
        for provider, tmdb_id, title, network, url, heading, sentence, status, season in cases:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                root = Path(folder)
                metadata = {"tmdbId": tmdb_id, "title": title,
                            "networks": [{"name": network}], "productionCompanies": []}
                article = f"<html><main><h1>{heading}</h1><p>{sentence}</p></main></html>"
                if provider == "AMC":
                    home = "https://www.amcglobalmedia.com/"
                    search = home + "?s=" + title.replace(" ", "+")
                    pages = {home: '<form method="get" action="/"><input name="s"></form>',
                             search: f'<h1>Search results</h1><a href="{url}">Official announcement</a>',
                             url: article}
                else:
                    sitemap = "https://press.disneyplus.com/sitemap.xml"
                    pages = {sitemap: ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                       f'<url><loc>{url}</loc></url></urlset>'), url: article}

                def fetch(requested, domains):
                    self.assertTrue(update.allowed(requested, domains))
                    if requested not in pages:
                        raise update.ProviderUnavailable("HTTP 404")
                    return pages[requested], requested

                def actual_discovery(meta, trusted):
                    return discovery.discover(meta, trusted, fetcher=fetch)

                kwargs = dict(dry_run=True, metadata=metadata, discoverer=actual_discovery,
                              fetcher=fetch, today=dt.date(2026, 10, 10),
                              now=dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc),
                              registry_path=root / "monitored.json", audit_path=root / "audit.jsonl")
                result = monitored.process(tmdb_id, **kwargs)
                self.assertEqual(result["provider"], provider)
                self.assertEqual(result["pipelineState"], "VERIFIED_FACTS", result)
                self.assertEqual(result["verifiedFacts"]["status"], status)
                self.assertEqual(result["verifiedFacts"]["nextSeasonNumber"], season)
                self.assertEqual(result["selectedOfficialUrl"], url)
                self.assertTrue(result["sourceChecks"][0]["redirectDomainAllowed"])
                self.assertTrue(result["candidateDiagnostics"][0]["factualParserEligible"])
                self.assertEqual(result["candidateDiagnostics"][0]["discoveryProvenance"],
                                 "amc_official_site_search" if provider == "AMC" else "disney_official_sitemap")
                self.assertEqual(result["validationResult"], "passed")
                self.assertEqual(result["registryChange"], "would_add")
                self.assertFalse((root / "monitored.json").exists())
                self.assertFalse((root / "audit.jsonl").exists())
                # The existing independent gate rebuilds the fact from the
                # proposed monitored evidence without using discovery output.
                row = result["proposedRecord"]
                facts = promotion._validate_evidence(row, discovery.provider_specs()[provider]["officialDomains"])
                self.assertTrue(facts)
                self.assertEqual(promotion._facts_summary(facts, row, dt.date(2026, 10, 10))["status"], status)
                self.assertEqual(monitored.process(tmdb_id, **kwargs)["proposedRecord"], row)
        self.assertEqual([path.read_bytes() for path in protected], before)


if __name__ == "__main__":
    unittest.main()
