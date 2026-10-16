"""V2.7.15 forensic regressions, not new coverage evidence.

Synthetic HTML/index containers exercise frozen corpus expectations and current
production gates. They reproduce reported selection/context blockers; they do
not model unobserved live search results or assert coverage recovery.
"""
import datetime as dt
from html import escape
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import discovery
import hbo_max_discovery as generic
import hbo_production
import max_production
import provider_coverage_audit as audit
import update

TODAY = dt.date(2026, 10, 15)
CORPUS = audit.load_corpus()
PROVIDERS = {p['provider']: p for p in CORPUS['providers']}
CASES = {c['caseId']: c for p in CORPUS['providers'] for c in p['cases']}
HEADINGS = {
 'hbo-the-last-of-us-3-1': 'HBO renueva THE LAST OF US por una tercera temporada',
 'hbo-rooster-2-2': 'HBO renueva ROOSTER por una segunda temporada',
 'hbo-it-welcome-to-derry-2-3': 'HBO renueva IT: BIENVENIDOS A DERRY para una segunda temporada',
 'hbo-the-comeback-3-4': 'La serie original de HBO THE COMEBACK vuelve con una tercera y última temporada',
 'max-tokyo-vice-2-1': 'HBO Max renueva la serie TOKYO VICE por una segunda temporada',
 'max-and-just-like-that-2-2': 'HBO Max renueva su serie AND JUST LIKE THAT... por una segunda temporada',
 'max-hacks-5-3': 'La serie original de Max HACKS vuelve con una quinta y última temporada',
 'max-hacks-4-6': 'Max renueva HACKS por una cuarta temporada',
}
NEWER = {
 'hbo-the-comeback-3-4': ('https://press-spain.max.com/post/la-tercera-temporada-de-la-serie-comica-original-de-hbo-the-come', 'La serie original de HBO THE COMEBACK vuelve con una tercera y última temporada', '2026-03-03'),
 'max-hacks-4-6': (CASES['max-hacks-5-3']['canonicalUrl'], HEADINGS['max-hacks-5-3'], '2026-03-23'),
}


def document(url, heading, date, body=None):
    return (f'<html><head><meta property="article:published_time" content="{date}">'
            f'<link rel="canonical" href="{escape(url)}"></head><body>'
            f'<h1>{escape(heading)}</h1><main><p>{escape(body or heading)}</p></main></body></html>')


def metadata(case, localized=False):
    title = 'And Just Like That…' if case['tmdbId'] == 116450 else case['title']
    provider = next(p['provider'] for p in CORPUS['providers'] if case in p['cases'])
    result = {'tmdbId': case['tmdbId'], 'title': title, 'originalName': title,
              'networks': [{'name': 'HBO' if provider == 'HBO' else 'Max'}]}
    if localized:
        result['hboSpanishTitle'] = 'IT: Bienvenidos a Derry'
    return result


def transport_for(case, *, include_gold=True, include_newer=False):
    heading = HEADINGS[case['caseId']]
    url = case['canonicalUrl']
    docs = {url: document(url, heading, case['expectedFacts']['announcementDate'])}
    links = [(url, heading)] if include_gold else []
    if include_newer:
        newer, title, date = NEWER[case['caseId']]
        docs[newer] = document(newer, title, date)
        links.append((newer, title))
    index = '<h1>Comunicados</h1>' + ''.join(f'<a href="{u}">{escape(t)}</a>' for u, t in links)
    calls = []
    def fetch(url, domains, headers=None):
        calls.append(url)
        if domains == ['api.themoviedb.org']:
            meta = metadata(case)
            return json.dumps({'id': meta['tmdbId'], 'name': meta['title'],
                               'original_name': meta['originalName'], 'networks': meta['networks']}), url
        if domains != ['press-spain.max.com']:
            raise update.AutomationError('fixture source boundary')
        return docs.get(url, index if url in generic.ENTRY_POINTS else '<h1>Comunicados</h1>'), url
    return fetch, calls


class RecoveryForensicsTests(unittest.TestCase):
    def setUp(self):
        self.specs = discovery.provider_specs()
        before = audit.protected_hashes()
        self.addCleanup(lambda: self.assertEqual(before, audit.protected_hashes()))

    def run_gold(self, key, *, localized=False):
        case = CASES[key]
        provider = 'HBO' if key.startswith('hbo-') else 'MAX'
        fetch, _ = transport_for(case)
        return audit._run_pipeline(provider, case, metadata(case, localized),
            fetcher=audit.BoundedFetcher(provider, transport=fetch), specs=self.specs,
            direct_diagnostic=True, today=TODAY)

    def run_generic(self, key, *, include_gold=True, include_newer=False):
        case = CASES[key]
        provider = 'HBO' if key.startswith('hbo-') else 'MAX'
        fetch, calls = transport_for(case, include_gold=include_gold, include_newer=include_newer)
        row = audit._case_result(PROVIDERS[provider], case, token='fixture-token',
                                 specs=self.specs, transport=fetch, today=TODAY)
        return row, calls

    def test_h1_existing_production_localized_identity_passes(self):
        row = self.run_gold('hbo-it-welcome-to-derry-2-3', localized=True)
        self.assertEqual(row['pipelineState'], 'VERIFIED_FACTS')
        self.assertTrue(row['reconstructionPassed'])
        self.assertEqual(row['reconstructedFacts'], CASES['hbo-it-welcome-to-derry-2-3']['expectedFacts'])

    def test_h1_frozen_audit_english_identity_is_not_translated_or_guessed(self):
        row = self.run_gold('hbo-it-welcome-to-derry-2-3')
        self.assertEqual(row['pipelineState'], 'VALIDATION_FAILED')
        self.assertFalse(row['reconstructionPassed'])
        case = CASES['hbo-it-welcome-to-derry-2-3']
        raw = json.dumps({'id': case['tmdbId'], 'name': case['title'], 'networks': [{'name': 'HBO'}]})
        meta = audit._fresh_metadata(case['tmdbId'], 'fixture-token', lambda *args: (raw, args[0]))
        self.assertNotIn('hboSpanishTitle', meta)

    def test_h2_original_article_alone_replays_all_seven_gold_fields(self):
        row = self.run_gold('hbo-the-comeback-3-4')
        self.assertTrue(row['reconstructionPassed'])
        self.assertEqual(row['reconstructedFacts'], CASES['hbo-the-comeback-3-4']['expectedFacts'])

    def test_h2_newer_valid_source_is_retained_instead_of_forcing_old_gold(self):
        row, _ = self.run_generic('hbo-the-comeback-3-4', include_newer=True)
        self.assertTrue(row['genericDiscovery'])
        self.assertTrue(row['reconstructionPassed'])
        self.assertEqual(row['reconstructedFacts']['announcementDate'], '2026-03-03')
        self.assertEqual(row['reconstructedFacts']['sourceUrl'], NEWER['hbo-the-comeback-3-4'][0])
        self.assertEqual(row['goldMismatchFields'], ['sourceUrl', 'announcementDate'])
        self.assertFalse(row['covered'])

    def test_m1_direct_article_is_compatible_without_claiming_search_success(self):
        row = self.run_gold('max-and-just-like-that-2-2')
        self.assertTrue(row['reconstructionPassed'])
        self.assertEqual(row['reconstructedFacts'], CASES['max-and-just-like-that-2-2']['expectedFacts'])

    def test_m1_no_index_evidence_never_uses_gold_url_as_generic_seed(self):
        row, calls = self.run_generic('max-and-just-like-that-2-2', include_gold=False)
        self.assertTrue(row['directUrlDiagnostic']['parserCompatible'])
        self.assertFalse(row['genericDiscovery'])
        self.assertEqual(row['primaryFailure'], 'DISCOVERY_MISSED')
        self.assertFalse(row['covered'])
        self.assertEqual(calls.count(CASES['max-and-just-like-that-2-2']['canonicalUrl']), 1)

    def test_m1_observed_index_can_discover_same_title_with_unicode_ellipsis(self):
        row, _ = self.run_generic('max-and-just-like-that-2-2')
        self.assertTrue(row['genericDiscovery'])
        self.assertTrue(row['covered'])

    def test_m2_historical_s4_article_alone_binds_s4_and_replays_gold(self):
        row = self.run_gold('max-hacks-4-6')
        self.assertTrue(row['reconstructionPassed'])
        self.assertEqual(row['reconstructedFacts'], CASES['max-hacks-4-6']['expectedFacts'])

    def test_m2_newer_explicit_s5_final_fact_is_not_downgraded_to_s4(self):
        row, _ = self.run_generic('max-hacks-4-6', include_newer=True)
        self.assertTrue(row['genericDiscovery'])
        self.assertTrue(row['reconstructionPassed'])
        self.assertEqual(row['reconstructedFacts']['nextSeasonNumber'], 5)
        self.assertEqual(row['reconstructedFacts']['status'], 'FINAL_SEASON')
        self.assertEqual(set(row['goldMismatchFields']), {'status', 'nextSeasonNumber', 'sourceUrl', 'announcementDate'})
        self.assertEqual(row['primaryFailure'], 'SEASON_BINDING_FAILED')
        self.assertFalse(row['covered'])

    def test_all_four_blocker_contracts_are_preserved_on_frozen_eight_case_model(self):
        counts = {}
        for provider in ('HBO', 'MAX'):
            rows = []
            for case in PROVIDERS[provider]['cases']:
                if case['kind'] != 'positive':
                    continue
                row, _ = self.run_generic(case['caseId'],
                    include_gold=case['caseId'] != 'max-and-just-like-that-2-2',
                    include_newer=case['caseId'] in NEWER)
                rows.append(row)
            counts[provider] = (sum(r['genericDiscovery'] for r in rows), sum(r['covered'] for r in rows), len(rows))
        # Synthetic contract-model replay, not measured live coverage.
        self.assertEqual(counts, {'HBO': (3, 2, 4), 'MAX': (3, 2, 4)})

    def test_rooster_and_tokyo_vice_remain_renewed_s2(self):
        for key in ('hbo-rooster-2-2', 'max-tokyo-vice-2-1'):
            with self.subTest(case=key):
                row = self.run_gold(key)
                self.assertTrue(row['reconstructionPassed'])
                self.assertEqual(row['reconstructedFacts']['status'], 'RENEWED')
                self.assertEqual(row['reconstructedFacts']['nextSeasonNumber'], 2)
                self.assertEqual(row['reconstructedFacts']['sourceName'], 'Max Press Spain')
                self.assertIsNone(row['reconstructedFacts']['releaseDate'])
                self.assertIsNone(row['reconstructedFacts']['releaseYear'])

    def test_strict_routes_and_inactive_fx_are_not_changed_for_coverage(self):
        for labels, route in ((['HBO'], 'HBO'), (['Max'], 'MAX'), (['HBO Max'], 'MAX'),
                              (['HBO', 'Max'], None), (['HBO', 'BBC'], None), (['FX'], None)):
            with self.subTest(labels=labels):
                self.assertEqual(discovery.route_provider({'networks': [{'name': x} for x in labels]}, self.specs)[0], route)
        self.assertNotIn('FX', update.ADAPTERS)

    def test_wrong_title_canonical_and_provider_boundaries_fail_closed(self):
        for provider in (hbo_production, max_production):
            key = 'hbo-rooster-2-2' if provider is hbo_production else 'max-tokyo-vice-2-1'
            case = CASES[key]; entry = {'title': case['title'], 'aliases': [case['title']]}
            wrong = document(case['canonicalUrl'], 'La serie original de HBO ANOTHER SHOW vuelve con una tercera y última temporada', '2026-03-01')
            with self.assertRaises(update.AutomationError):
                provider.validate_article(wrong, case['canonicalUrl'], case['canonicalUrl'], entry)
            with self.assertRaises(update.AutomationError):
                provider.validate_article(wrong, case['canonicalUrl'], 'https://untrusted.example/post/x', entry)

    def test_non_lifecycle_and_wrong_title_news_do_not_become_facts(self):
        negatives = (
            'Tráiler de la tercera temporada de SERIES A.',
            'Teaser oficial de SERIES A.',
            'Nuevas incorporaciones al reparto de SERIES A.',
            'SERIES A comienza el rodaje de su tercera temporada.',
            'El episodio final de la temporada de SERIES A.',
            'SERIES A recibe ocho premios.',
            'Entrevista con el reparto de SERIES A.',
            'Resumen de la primera temporada de SERIES A.',
            'La franquicia de OTHER SHOW presenta un nuevo personaje.',
            'HBO renueva OTHER SHOW por una cuarta temporada. SERIES A presenta entrevistas.',
            'SERIES A tiene episodios de las temporadas primera, segunda y tercera.',
            'HBO renueva SERIES AB por una tercera temporada.',
            'Fotos de producción de SERIES A.',
        )
        entry = {'title': 'SERIES A', 'aliases': ['SERIES A']}
        for provider, brand in ((hbo_production, 'HBO'), (max_production, 'Max')):
            for text in negatives:
                with self.subTest(provider=brand, text=text):
                    article = {'title': f'La serie original de {brand} SERIES A', 'body': text,
                        'url': 'https://press-spain.max.com/post/series-a-news',
                        'sourceName': 'Max Press Spain', 'publicationDate': '2026-03-01',
                        'language': 'es', 'territory': 'ES'}
                    self.assertEqual(provider.detect(article, entry, TODAY), [])


if __name__ == '__main__':
    unittest.main()
