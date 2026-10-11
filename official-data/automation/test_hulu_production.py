"""Offline activation integration tests; all writes target isolated fixtures."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

import discovery
import hulu_discovery
import monitored
import promotion
import update
from test_shrill_reconstruction import SHRILL_HTML, SHRILL_URL
from test_hulu_discovery_audit import generic_renewal_article
from test_hulu_binding_regression import DELI_URL

TODAY = dt.date(2026, 10, 11)
NOW = dt.datetime(2026, 10, 11, 12, tzinfo=dt.timezone.utc)
FIXTURES = Path(__file__).parent / 'fixtures'
PROTECTED = ['official-data/official_series_data.json', 'official-data/history/changes.jsonl',
             'official-data/sources.json', 'official-data/monitored_series.json',
             'official-data/history/monitored_changes.jsonl']


def metadata(title='Deli Boys', **values):
    return {'tmdbId': 900123, 'title': title, 'networks': [{'name': 'Hulu'}],
            'productionCompanies': [], 'homepage': '', **values}


def html(title, body):
    return (f'<html><head><title>{title}</title>'
            '<meta property="article:published_time" content="2025-08-26"></head>'
            f'<body><h1>{title}</h1><article><p>{body}</p></article></body></html>')


class HuluProductionTests(unittest.TestCase):
    def setUp(self):
        self.specs = discovery.provider_specs()
        self.trusted = json.loads(update.REGISTRY.read_text(encoding='utf-8'))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = self.root / 'monitored.json'
        self.stage_audit = self.root / 'monitored.jsonl'
        self.data = self.root / 'canonical.json'
        self.audit = self.root / 'canonical.jsonl'
        self.registry = self.root / 'sources.json'
        self.data.write_bytes((FIXTURES / 'v235_canonical.json').read_bytes())
        self.audit.write_bytes(b'')
        self.registry.write_text(json.dumps(self.trusted), encoding='utf-8')
        self.live_before = {p: hashlib.sha256((update.ROOT / p).read_bytes()).hexdigest() for p in PROTECTED}
        self.addCleanup(self.assert_live_unchanged)

    def assert_live_unchanged(self):
        self.assertEqual(self.live_before, {p: hashlib.sha256((update.ROOT / p).read_bytes()).hexdigest() for p in PROTECTED})

    def fetcher(self, title, url, raw, *, context=False):
        calls = []
        def fetch(requested, domains):
            calls.append(requested)
            self.assertEqual(domains, ['press.hulu.com'])
            self.assertTrue(update.allowed(requested, domains))
            if requested == url:
                return raw, requested
            if requested == 'https://press.hulu.com/':
                return '<html><title>Hulu Search</title><form action="/" method="get"><input name="s"></form></html>', requested
            query = parse_qs(urlparse(requested).query).get('s', [None])[0]
            if context:
                if query == title + ' season':
                    return f'<html><title>Results</title><a href="{url}">Hulu corporate announcement</a></html>', requested
                return '<html><title>Hulu News</title></html>', requested
            return f'<html><title>Hulu News</title><a href="{url}">{title} season announcement</a></html>', requested
        return fetch, calls

    def process(self, meta=None, raw=None, url=DELI_URL, *, context=False, discoverer=None, dry_run=False):
        meta = meta or metadata()
        raw = raw or html('HULU\u2019S COMEDY SERIES \u2018DELI BOYS\u2019 RENEWED FOR A SECOND SEASON',
                          '\u201cDeli Boys\u201d has been renewed for a second season.')
        fetch, calls = self.fetcher(meta['title'], url, raw, context=context)
        discoverer = discoverer or (lambda m, r: discovery.discover(m, r, fetcher=fetch))
        result = monitored.process(meta['tmdbId'], dry_run=dry_run, metadata=meta,
                                   discoverer=discoverer, fetcher=fetch, today=TODAY, now=NOW,
                                   registry_path=self.stage, audit_path=self.stage_audit)
        return result, calls

    def promote(self):
        before = self.data.read_bytes(), self.audit.read_bytes()
        result = promotion.promote(900123, dry_run=True, now=NOW, production_data=self.data,
                                   production_audit=self.audit, production_registry=self.registry,
                                   monitored_registry=self.stage)
        self.assertEqual(before, (self.data.read_bytes(), self.audit.read_bytes()))
        return result

    def tamper(self, change):
        staging = json.loads(self.stage.read_text(encoding='utf-8'))
        change(staging['series'][0])
        self.stage.write_text(json.dumps(staging), encoding='utf-8')

    def test_hulu_network_is_eligible_and_fx_is_not_registered(self):
        self.assertEqual(discovery.route_provider(metadata(), self.specs)[0], 'HULU')
        self.assertIn('HULU', update.ADAPTERS)
        self.assertNotIn('FX', self.specs)
        self.assertNotIn('FX', update.ADAPTERS)
        result, calls = self.process(metadata(networks=[{'name': 'FX'}]))
        self.assertEqual(result['pipelineState'], 'UNSUPPORTED_PROVIDER')
        self.assertEqual(calls, [])

    def test_ownership_distributor_homepage_or_url_is_not_hulu_identity(self):
        for fields in [
            {'networks': [], 'productionCompanies': [{'name': 'Hulu'}]},
            {'networks': [], 'homepage': DELI_URL},
            {'networks': [{'name': 'ABC'}], 'productionCompanies': [{'name': 'Hulu'}]},
            {'networks': [], 'distributor': 'Hulu', 'owner': 'Disney'},
            {'networks': [{'name': 'Hulu Plus'}]},
        ]:
            with self.subTest(fields=fields):
                provider, _, _ = discovery.route_provider(metadata(**fields), self.specs)
                self.assertNotEqual(provider, 'HULU')

    def test_ambiguous_provider_identity_does_not_fetch(self):
        result, calls = self.process(metadata(networks=[{'name': 'Hulu'}, {'name': 'Netflix'}]))
        self.assertEqual(result['pipelineState'], 'DISCOVERY_INSUFFICIENT')
        self.assertEqual(calls, [])

    def test_existing_provider_routing_is_unchanged(self):
        old_specs = {k: v for k, v in self.specs.items() if k != 'HULU'}
        for network in ['AMC', 'AMC+', 'Disney+', 'Disney Plus', 'Disney', 'Netflix',
                        'Paramount+', 'Prime Video', 'Apple TV', 'HBO', 'FX']:
            for companies in [[], [{'name': 'Amazon Studios'}], [{'name': 'Disney'}]]:
                meta = metadata(networks=[{'name': network}], productionCompanies=companies)
                self.assertEqual(discovery.route_provider(meta, self.specs),
                                 discovery.route_provider(meta, old_specs))

    def test_deli_full_production_flow_and_seven_field_replay(self):
        result, _ = self.process()
        self.assertEqual(result['pipelineState'], 'VERIFIED_FACTS', result)
        self.assertEqual(result['provider'], 'HULU')
        row = json.loads(self.stage.read_text())['series'][0]
        facts = promotion._validate_evidence(row, ['press.hulu.com'])
        reconstructed = promotion._facts_summary(facts, row, TODAY)
        self.assertEqual(len(monitored.FACT_FIELDS), 7)
        self.assertEqual({k: reconstructed.get(k) for k in monitored.FACT_FIELDS}, row['verifiedFacts'])
        promoted = self.promote()
        self.assertTrue(promoted['promotionEligible'], promoted)
        self.assertTrue(promoted['wouldPublish'], promoted)
        self.assertEqual(promoted['proposedProductionRecord']['status'], 'RENEWED')
        self.assertEqual(promoted['proposedProductionRecord']['nextSeasonNumber'], 2)

    def test_all_five_qualified_cases_use_the_production_adapter(self):
        cases = [
            ('Deli Boys', DELI_URL,
             html('HULU\u2019S COMEDY SERIES \u2018DELI BOYS\u2019 RENEWED FOR A SECOND SEASON',
                  'Actor biography describes the eighth and final season of Big Mouth.'), 'RENEWED', 2, False),
            ('Difficult People', 'https://press.hulu.com/pressrelease/hulu-renews-difficult-people-for-season-two/',
             html('Hulu Renews Difficult People for Season Two',
                  'Hulu Original Difficult People has been picked up for a second season.'), 'RENEWED', 2, False),
            ("The Handmaid's Tale", 'https://press.hulu.com/pressrelease/hulu-surges-past-20-million-u-s-subscribers-and-announces-plans-to-offer-advertising-in-live-tv-this-quarter/',
             generic_renewal_article(), 'RENEWED', 3, True),
            ('Shrill', SHRILL_URL, SHRILL_HTML.replace('"Shrill"', '\u201cShrill\u201d'), 'FINAL_SEASON', 3, False),
            ('Reasonable Doubt', 'https://press.hulu.com/pressrelease/reasonable-doubt-season-four-cast/',
             html('Reasonable Doubt Season 4 cast announcement', 'New actors join Season 4 of Reasonable Doubt.'), None, None, False),
        ]
        for title, url, raw, status, season, context in cases:
            with self.subTest(title=title):
                result, calls = self.process(metadata(title), raw, url, context=context)
                if status is None:
                    self.assertEqual(result['pipelineState'], 'NO_VERIFIED_FACTS', result)
                    self.assertEqual(result['verifiedFacts'], {})
                    self.assertFalse(self.promote()['promotionEligible'])
                else:
                    self.assertEqual(result['pipelineState'], 'VERIFIED_FACTS', result)
                    self.assertEqual((result['verifiedFacts']['status'], result['verifiedFacts']['nextSeasonNumber']), (status, season))
                    self.assertTrue(self.promote()['promotionEligible'])
                    if context:
                        self.assertIn('hulu_official_site_search_context',
                                      [c['discoveryProvenance'] for c in result['candidateDiagnostics']])
                        queries = [parse_qs(urlparse(c).query).get('s', [None])[0] for c in calls]
                        self.assertIn(title + ' season', queries)
                        self.assertFalse(any('three' in q or 'renewed' in q for q in queries if q))

    def test_multititle_negation_speculation_and_cast_only_have_no_lifecycle(self):
        for body in [
            'Deli Boys returns for season four, while the third and final season of Other Show premieres later.',
            'Deli Boys has not been renewed for season two.',
            'Deli Boys could return for season two.',
            'Deli Boys may return for season two.',
            'Deli Boys is expected to return for season two.',
            'Deli Boys reportedly renewed for season two.',
            'Deli Boys season two cast joins the show.',
        ]:
            with self.subTest(body=body):
                result, _ = self.process(raw=html('Deli Boys season news', body))
                self.assertEqual(result['pipelineState'], 'NO_VERIFIED_FACTS', result)
                self.assertFalse(self.promote()['promotionEligible'])

    def test_third_party_result_and_official_redirect_outside_boundary_rejected(self):
        url = 'https://press.hulu.com/'
        for final in ['https://third-party.example/pressrelease/deli-boys/',
                      'https://press.hulu.com.evil.example/pressrelease/deli-boys/']:
            calls = []
            def fetch(requested, domains):
                calls.append(requested)
                return html('Deli Boys renewed for season two', ''), final
            report = discovery.discover(metadata(), self.trusted, fetcher=fetch)
            self.assertEqual(report['candidateUrls'], [])
            self.assertTrue(all(update.allowed(c, ['press.hulu.com']) for c in calls))

    def test_wrong_domain_in_stored_evidence_blocks_promotion(self):
        self.process()
        self.tamper(lambda row: row['sourceEvidence'][0].update(sourceUrl='https://third-party.example/story'))
        result = self.promote()
        self.assertFalse(result['promotionEligible'])
        self.assertEqual(result['validationResult'], 'failed')

    def test_each_of_seven_summary_field_mismatches_blocks_promotion(self):
        changed = {'status': 'FINAL_SEASON', 'nextSeasonNumber': 3,
                   'releaseDate': '2027-05-01', 'releaseYear': 2027,
                   'sourceName': 'Wrong source', 'sourceUrl': 'https://press.hulu.com/other',
                   'announcementDate': '2025-08-27'}
        self.process()
        baseline = self.stage.read_bytes()
        for key, value in changed.items():
            with self.subTest(field=key):
                self.stage.write_bytes(baseline)
                self.tamper(lambda row: row['verifiedFacts'].update({key: value}))
                result = self.promote()
                self.assertFalse(result['promotionEligible'], result)
                self.assertEqual(result['validationResult'], 'failed')

    def test_independent_summary_compares_every_promotion_field_exactly(self):
        self.process()
        row = json.loads(self.stage.read_text())['series'][0]
        facts = promotion._validate_evidence(row, ['press.hulu.com'])
        for key in monitored.FACT_FIELDS:
            changed = copy.deepcopy(row)
            changed['verifiedFacts'][key] = 'synthetic-mismatch'
            with self.subTest(field=key), self.assertRaises(promotion.EvidenceMismatch) as error:
                promotion._facts_summary(facts, changed, TODAY)
            self.assertEqual(error.exception.diagnostic['fields'], [key])

    def test_missing_or_wrong_persisted_excerpt_blocks_promotion(self):
        for text in ['', 'Another Show has been renewed for season two.']:
            self.process()
            self.tamper(lambda row: row['sourceEvidence'][0].update(evidenceText=text))
            self.assertFalse(self.promote()['promotionEligible'])

    def test_wrong_tmdb_network_cannot_use_injected_eligible_hulu_candidate(self):
        def injected(meta, registry):
            return {'provider': 'HULU', 'result': 'candidate_found',
                    'candidates': [{'candidateUrl': DELI_URL, 'factualParserEligible': True}],
                    'candidateUrls': [DELI_URL]}
        result, calls = self.process(metadata(networks=[{'name': 'FX'}]), discoverer=injected)
        self.assertEqual(result['pipelineState'], 'VALIDATION_FAILED')
        self.assertEqual(calls, [])
        self.assertFalse(self.stage.exists())

    def test_refetch_changed_identity_is_rejected(self):
        def injected(meta, registry):
            return {'provider': 'HULU', 'result': 'candidate_found',
                    'candidates': [{'candidateUrl': DELI_URL, 'factualParserEligible': True}],
                    'candidateUrls': [DELI_URL]}
        result, _ = self.process(raw=html('Another Show renewed for season two',
                                         'Another Show has been renewed for season two.'), discoverer=injected)
        self.assertEqual(result['pipelineState'], 'VALIDATION_FAILED')

    def test_dry_run_never_creates_even_a_temporary_monitored_record(self):
        result, _ = self.process(dry_run=True)
        self.assertEqual(result['pipelineState'], 'VERIFIED_FACTS')
        self.assertFalse(self.stage.exists())
        self.assertFalse(self.stage_audit.exists())

    def test_fetch_budget_and_two_queries_are_bounded(self):
        calls = []
        def fetch(url, domains):
            calls.append(url)
            if url == 'https://press.hulu.com/':
                return '<html><title>Search</title><form action="/"><input name="s"></form></html>', url
            if '?s=' in url:
                links = ''.join(f'<a href="/pressrelease/other-{i}">Other Show</a>' for i in range(100))
                return '<html><title>Results</title>' + links + '</html>', url
            return html('Other Show season news', 'Other Show season two cast.'), url
        report = discovery.discover(metadata(), self.trusted, fetcher=fetch)
        self.assertEqual(report['candidateUrls'], [])
        self.assertLessEqual(len(calls), hulu_discovery.MAX_FETCHES)
        self.assertEqual(sum('?s=' in c for c in calls), 2)


if __name__ == '__main__':
    unittest.main()
