"""Offline MAX production integration regressions; all writes stay in temp files."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from html import escape
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import automatic_promotion
import discovery
import max_production as max_prod
import monitored
import promotion
import update

FIXTURE = Path(__file__).parent / "fixtures" / "v2711_max_production.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
NOW = dt.datetime(2026, 10, 13, 20, tzinfo=dt.timezone.utc)
PROTECTED = (
    "official-data/official_series_data.json",
    "official-data/history/changes.jsonl",
    "official-data/sources.json",
    "official-data/monitored_series.json",
    "official-data/history/monitored_changes.jsonl",
)


def article_html(case, *, heading=None, evidence=None, date=None, canonical=None,
                 body_extra=""):
    heading = heading or case["heading"]
    evidence = evidence or case["evidenceText"]
    date = date or case["announcementDate"]
    canonical = canonical or case["url"]
    # The runner confirmed MAX brand identity for each case. Hacks carries a
    # brand-context sentence alongside its separate lifecycle excerpt.
    brand = f"La serie original de Max, {case['title']}." if case["title"] == "Hacks" else ""
    jsonld = json.dumps({"@type": "NewsArticle", "headline": heading,
                         "url": canonical, "datePublished": date})
    return (f'<html><head><meta property="article:published_time" content="{date}">'
            f'<link rel="canonical" href="{escape(canonical)}">'
            f'<script type="application/ld+json">{jsonld}</script></head><body>'
            f'<h1>{escape(heading)}</h1><main><p>{escape(brand)}</p>'
            f'<p>{escape(evidence)}</p><p>{escape(body_extra)}</p></main></body></html>')


class MaxProductionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stage = self.root / "monitored.json"
        self.stage_audit = self.root / "monitored.jsonl"
        self.data = self.root / "canonical.json"
        self.history = self.root / "changes.jsonl"
        self.registry = self.root / "sources.json"
        self.data.write_bytes((Path(__file__).parent / "fixtures" / "v235_canonical.json").read_bytes())
        self.history.write_bytes(b"")
        self.registry.write_bytes(update.REGISTRY.read_bytes())
        self.specs = discovery.provider_specs()
        before = {path: hashlib.sha256((update.ROOT / path).read_bytes()).hexdigest()
                  for path in PROTECTED}
        self.addCleanup(lambda: self.assertEqual(before, {
            path: hashlib.sha256((update.ROOT / path).read_bytes()).hexdigest()
            for path in PROTECTED}))

    def metadata(self, case, networks=None):
        return {"tmdbId": case["tmdbId"], "title": case["title"],
                "originalName": case["title"],
                "networks": [{"name": name} for name in (networks or case["networks"])]}

    def pages(self, case, **article_options):
        target = case["url"]
        index = f'<html><h1>Comunicados</h1><a href="{target}">{case["title"]} renovación temporada</a></html>'
        return {"https://press-spain.max.com/": index,
                "https://press-spain.max.com/sitemap": "<html><h1>Mapa del sitio</h1></html>",
                "https://press-spain.max.com/post": "<html><h1>Notas de prensa</h1></html>",
                target: article_html(case, **article_options)}

    def process(self, case, *, pages=None, metadata=None, dry=False, fetcher=None):
        pages = pages if pages is not None else self.pages(case)
        calls = []

        def fetch(url, domains):
            self.assertEqual(domains, [max_prod.HOST])
            calls.append(url)
            if url not in pages:
                return "<html><h1>Índice de prensa</h1></html>", url
            item = pages[url]
            if isinstance(item, Exception):
                raise item
            return item, url

        fetch = fetcher or fetch
        meta = metadata or self.metadata(case)
        report = lambda m, r: discovery.discover(m, r, fetcher=fetch)
        result = monitored.process(case["tmdbId"], dry_run=dry, metadata=meta,
                                   discoverer=report, fetcher=fetch, now=NOW, today=NOW.date(),
                                   registry_path=self.stage, audit_path=self.stage_audit)
        return result, calls

    def row(self, tmdb_id):
        data = json.loads(self.stage.read_text(encoding="utf-8"))
        return next(item for item in data["series"] if item["tmdbId"] == tmdb_id)

    def promote(self, tmdb_id):
        before = (self.data.read_bytes(), self.history.read_bytes())
        result = promotion.promote(tmdb_id, dry_run=True, now=NOW,
                                   production_data=self.data, production_audit=self.history,
                                   production_registry=self.registry, monitored_registry=self.stage)
        self.assertEqual(before, (self.data.read_bytes(), self.history.read_bytes()))
        return result

    def test_strict_routing(self):
        expected = [(["Max"], "MAX"), (["HBO Max"], "MAX"), (["HBO"], "HBO"),
                    (["HBO", "Max"], None), (["Unknown Provider"], None),
                    (["Max", "Netflix"], None), (["HBO Max", "BBC One"], None)]
        for labels, route in expected:
            with self.subTest(labels=labels):
                actual, _, reason = discovery.route_provider(
                    {"tmdbId": 7, "title": "Example", "networks": labels}, self.specs)
                self.assertEqual(actual, route)
                if labels == ["HBO", "Max"]:
                    self.assertEqual(reason, "ambiguous_provider")
        self.assertEqual(max_prod.strict_network(self.metadata(CASES[0])), True)
        with self.assertRaises(update.AutomationError):
            max_prod.identity_context(self.metadata(CASES[0], ["HBO", "Max"]))
        for extra in ({"productionCompanies": [{"name": "Max"}]},
                      {"homepage": "https://press-spain.max.com/"},
                      {"studio": "Max", "distributor": "Max"}):
            meta = {"tmdbId": 8, "title": "Example", "networks": [], **extra}
            self.assertNotEqual(discovery.route_provider(meta, self.specs)[0], "MAX")

    def test_default_transport_allows_only_qualified_host_and_safe_redirects(self):
        root = "https://press-spain.max.com/sitemap"
        with patch.object(max_prod.qualified, "fetch", return_value=(
                {"status": "fetched", "finalUrl": root}, "<html><h1>Index</h1></html>")) as transport:
            raw, final = max_prod.fetch(root, [max_prod.HOST])
        self.assertIn("Index", raw)
        self.assertEqual(final, root)
        transport.assert_called_once_with(root, [max_prod.HOST])
        with patch.object(max_prod.qualified, "fetch", return_value=(
                {"status": "fetched", "finalUrl": "https://example.com/"}, "<html><h1>Index</h1></html>")):
            with self.assertRaises(update.AutomationError):
                max_prod.fetch(root, [max_prod.HOST])
        self.assertIsNone(max_prod.canonical("https://sub.press-spain.max.com/post/example"))

    def test_known_positive_url_is_not_fetched_without_generic_index_link(self):
        case = CASES[0]
        pages = {"https://press-spain.max.com/": "<html><h1>Portada</h1></html>",
                 "https://press-spain.max.com/sitemap": "<html><h1>Sitemap</h1></html>",
                 "https://press-spain.max.com/post": "<html><h1>Notas</h1></html>",
                 case["url"]: article_html(case)}
        result, calls = self.process(case, pages=pages)
        self.assertNotIn(case["url"], calls)
        self.assertEqual(result["pipelineState"], "DISCOVERY_INSUFFICIENT")

    def test_unsafe_article_redirect_cannot_become_candidate(self):
        case = CASES[0]
        pages = self.pages(case)
        calls = []
        def redirect(url, domains):
            calls.append(url)
            return pages.get(url, "<html><h1>Index</h1></html>"), (
                "https://evil.example/article" if url == case["url"] else url)
        result, _ = self.process(case, pages=pages, fetcher=redirect)
        self.assertNotIn(case["url"], result["candidateUrls"])
        self.assertNotEqual(result["pipelineState"], "VERIFIED_FACTS")


    def test_qualified_runner_fixtures_discover_parse_replay_and_promotion(self):
        self.assertEqual(json.loads(FIXTURE.read_text(encoding="utf-8"))["sourceArtifactSha256"],
                         "e27622edebe4926ea26a70b9622a6befd871d8376a2b8342373a14666d94a4c6")
        for case in CASES:
            with self.subTest(title=case["title"]):
                result, calls = self.process(case)
                self.assertEqual(result["pipelineState"], "VERIFIED_FACTS", result)
                self.assertEqual(result["provider"], "MAX")
                self.assertEqual(result["verifiedFacts"], {
                    "status": case["status"], "nextSeasonNumber": case["nextSeasonNumber"],
                    "releaseDate": None, "releaseYear": None, "sourceName": max_prod.SOURCE,
                    "sourceUrl": case["url"], "announcementDate": case["announcementDate"]})
                self.assertIn("https://press-spain.max.com/", calls)
                self.assertFalse(result["candidateDiagnostics"][0]["knownUrlUsedAsSeed"])
                row = self.row(case["tmdbId"])
                self.assertEqual(row["providerIdentity"]["networks"], self.metadata(case)["networks"])
                self.assertTrue(self.promote(case["tmdbId"])["promotionEligible"])
                self.assertEqual(len(row["sourceEvidence"]), 1)
                self.assertLessEqual(len(row["sourceEvidence"][0]["evidenceText"]), 200)
                if "regionalDate" in case:
                    self.assertIsNone(result["verifiedFacts"]["releaseDate"])
                    article = update.ADAPTERS["MAX"].parse(self.pages(case)[case["url"]], case["url"])
                    diagnostic = max_prod.regional_dates(article, {
                        "tmdbId": case["tmdbId"], "title": case["title"], "aliases": [case["title"]]})
                    self.assertEqual(diagnostic[0]["regionalReleaseDate"], case["regionalDate"])
                    self.assertIsNone(diagnostic[0]["productionReleaseDate"])

    def test_pitt_trailer_teaser_negation_and_speculation_are_not_facts(self):
        case = {"tmdbId": 250307, "title": "The Pitt", "networks": ["Max"],
                "url": "https://press-spain.max.com/post/the-pitt-fixture",
                "heading": "HBO Max lanza el teaser de la tercera temporada de The Pitt",
                "evidenceText": "HBO Max lanza el teaser de la tercera temporada de The Pitt",
                "announcementDate": "2026-02-01"}
        texts = (
            "HBO Max lanza el teaser de la tercera temporada de The Pitt",
            "The Pitt presenta el tráiler de su tercera temporada",
            "The Pitt no ha sido renovada por una tercera temporada",
            "The Pitt podría ser renovada por una tercera temporada",
            "The Pitt espera ser renovada por una tercera temporada",
            "The Pitt comienza el rodaje de su tercera temporada",
        )
        entry = {"tmdbId": case["tmdbId"], "title": case["title"], "aliases": [case["title"]]}
        for text in texts:
            with self.subTest(text=text):
                article = {"title": case["heading"], "body": text, "url": case["url"],
                           "sourceName": max_prod.SOURCE, "publicationDate": case["announcementDate"],
                           "language": "es", "territory": "ES"}
                self.assertEqual(max_prod.detect(article, entry, NOW.date()), [])

    def test_multi_title_and_cross_season_facts_remain_bound(self):
        case = copy.deepcopy(CASES[0])
        article = {"title": "", "body": "Tokyo Vice ha sido renovada por una segunda temporada. "
                   "Other Show ha sido renovada por una cuarta temporada. "
                   "La primera temporada de Tokyo Vice se estrena el 14 de abril.",
                   "url": case["url"], "sourceName": max_prod.SOURCE,
                   "publicationDate": case["announcementDate"], "language": "es", "territory": "ES"}
        facts = max_prod.detect(article, {"tmdbId": case["tmdbId"], "title": case["title"],
                                          "aliases": [case["title"]]}, NOW.date())
        self.assertEqual(len(facts), 1)
        self.assertEqual((facts[0]["status"], facts[0]["nextSeasonNumber"]), ("RENEWED", 2))
        self.assertIsNone(facts[0]["releaseDate"])
        self.assertIsNone(facts[0]["releaseYear"])
        bound_entry = {"tmdbId": case["tmdbId"], "title": case["title"],
                       "aliases": [case["title"]], "provider": "MAX"}
        self.assertEqual(update.detect(article, bound_entry, NOW.date()), facts)

    def test_seven_field_persisted_fact_mismatches_block_independent_replay(self):
        case = CASES[0]
        self.process(case)
        baseline = self.stage.read_bytes()
        fields = ("status", "nextSeasonNumber", "releaseDate", "releaseYear",
                  "sourceName", "sourceUrl", "announcementDate")
        wrong_values = {"status": "FINAL_SEASON", "nextSeasonNumber": 9,
                        "releaseDate": "2027-01-01", "releaseYear": 2027,
                        "sourceName": "Untrusted source", "sourceUrl": "https://example.com/article",
                        "announcementDate": "2022-06-08"}
        for field, value in wrong_values.items():
            with self.subTest(field=field):
                if field != "status":
                    self.stage.write_bytes(baseline)
                data = json.loads(baseline)
                row = next(item for item in data["series"] if item["tmdbId"] == case["tmdbId"])
                row["verifiedFacts"][field] = value
                self.stage.write_text(json.dumps(data), encoding="utf-8")
                result = self.promote(case["tmdbId"])
                self.assertFalse(result["promotionEligible"], result)
                self.assertEqual(result["validationResult"], "failed")
        self.stage.write_bytes(baseline)

    def test_source_url_announcement_date_route_and_untrusted_host_tampering_block(self):
        case = CASES[0]
        mutations = (
            ("sourceName", lambda row: row["verifiedFacts"].update(sourceName="Other Press")),
            ("sourceUrl", lambda row: row["verifiedFacts"].update(sourceUrl="https://example.com/a")),
            ("announcementDate", lambda row: row["verifiedFacts"].update(announcementDate="2022-06-08")),
            ("route", lambda row: row["providerIdentity"].update(networks=[{"name": "HBO"}])),
            ("mixed_route", lambda row: row["providerIdentity"].update(networks=[{"name": "HBO"}, {"name": "Max"}])),
            ("untrusted_host", lambda row: row["sourceEvidence"][0].update(sourceUrl="https://example.com/a")),
        )
        self.process(case)
        baseline = self.stage.read_bytes()
        for name, mutate in mutations:
            with self.subTest(name=name):
                self.stage.write_bytes(baseline)
                data = json.loads(baseline)
                row = next(item for item in data["series"] if item["tmdbId"] == case["tmdbId"])
                mutate(row)
                self.stage.write_text(json.dumps(data), encoding="utf-8")
                result = self.promote(case["tmdbId"])
                self.assertFalse(result["promotionEligible"], result)
        self.stage.write_bytes(baseline)

    def test_shared_gate_configuration_and_disabled_or_wrong_route_block(self):
        self.assertTrue(automatic_promotion.gate_enabled("true"))
        self.assertFalse(automatic_promotion.gate_enabled("TRUE"))
        workflow = (update.ROOT / ".github/workflows/process-series-request.yml").read_text(encoding="utf-8")
        self.assertIn("vars.OFFICIAL_DATA_PROMOTION_ENABLED", workflow)
        self.assertNotIn("process-max-series-request", workflow)
        case = CASES[0]
        self.process(case)
        disabled = copy.deepcopy(self.specs)
        disabled["MAX"]["productionActivated"] = False
        with patch.object(discovery, "provider_specs", return_value=disabled):
            self.assertFalse(self.promote(case["tmdbId"])["promotionEligible"])

    def test_hbo_provider_route_and_parser_remain_separate(self):
        hbo = {"tmdbId": 123, "title": "Rooster", "networks": [{"name": "HBO"}]}
        self.assertEqual(discovery.route_provider(hbo, self.specs)[0], "HBO")
        self.assertEqual(discovery.route_provider({**hbo, "networks": [{"name": "HBO Max"}]}, self.specs)[0], "MAX")
        self.assertIsNone(discovery.route_provider({**hbo, "networks": [{"name": "HBO"}, {"name": "Max"}]}, self.specs)[0])

    def test_provider_unavailable_preserves_existing_monitored_record(self):
        case = CASES[0]
        self.process(case)
        before = self.stage.read_bytes()
        def unavailable(url, domains):
            raise update.ProviderUnavailable("HTTP 403")
        result, _ = self.process(case, fetcher=unavailable)
        self.assertEqual(result["pipelineState"], "PROVIDER_UNAVAILABLE")
        self.assertEqual(result["verifiedFacts"], self.row(case["tmdbId"])["verifiedFacts"])
        self.assertEqual(before, self.stage.read_bytes())

    def test_refetch_unavailable_preserves_existing_verified_max_record(self):
        case = CASES[0]
        self.process(case)
        before = self.stage.read_bytes()
        pages = self.pages(case)
        def discoverer(meta, registry):
            return discovery.discover(meta, registry, fetcher=lambda url, domains: (
                pages.get(url, "<html><h1>Index</h1></html>"), url))
        def refetch_failure(url, domains):
            raise update.ProviderUnavailable("HTTP 403")
        result = monitored.process(case["tmdbId"], dry_run=False, metadata=self.metadata(case),
                                   discoverer=discoverer, fetcher=refetch_failure, now=NOW,
                                   today=NOW.date(), registry_path=self.stage, audit_path=self.stage_audit)
        self.assertEqual(result["pipelineState"], "PROVIDER_UNAVAILABLE")
        self.assertEqual(result["registryChange"], "none")
        self.assertEqual(before, self.stage.read_bytes())


if __name__ == "__main__":
    unittest.main()
