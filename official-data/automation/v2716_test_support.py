"""Stable V2.7.16 test support: SYNTHETIC_TEST_FIXTURE, not coverage evidence.

Extracted from the V2.7.15 forensic test helper without production logic.
Used by test_v2716_audit_fidelity; HBO/MAX synthetic announcement/index shapes
model the same four recorded scenarios. This module is packaged explicitly.

Synthetic HTML/index containers exercise frozen corpus expectations and current
production gates. They reproduce reported selection/context blockers; they do
not model unobserved live search results or assert coverage recovery.
"""
import datetime as dt
import discovery
from urllib.parse import parse_qs, urlsplit
from html import escape
import json
from pathlib import Path

import hbo_max_discovery as generic
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



FIXTURE = Path(__file__).parent / 'fixtures/v2716_identity_context.json'
CAPTURE = json.loads(FIXTURE.read_text(encoding='utf-8'))
IDENTITIES = {r['tmdbId']: r for r in CAPTURE['records']}
OPTIONS = {r['caseId']: r for r in CAPTURE['forensicCases']}


def replay_case(key, **overrides):
    case = CASES[key]
    provider = PROVIDERS['HBO' if key.startswith('hbo-') else 'MAX']
    options = {**OPTIONS.get(key, {}), **overrides}
    base, calls = transport_for(case,
        include_gold=options.get('includeGoldInIndex', True),
        include_newer=options.get('includeNewerInIndex', False))
    record = IDENTITIES[case['tmdbId']]
    def transport(url, domains, headers=None):
        if domains == ['api.themoviedb.org']:
            calls.append(url)
            language = parse_qs(urlsplit(url).query).get('language', ['en-US'])[0]
            return json.dumps(record['esES' if language == 'es-ES' else 'enUS']), url
        return base(url, domains, headers)
    transport.capture_provenance = record['provenance']
    specs = discovery.provider_specs()
    generic = audit.BoundedFetcher(provider['provider'], transport=transport)
    direct = audit.BoundedFetcher(provider['provider'], transport=transport)
    legacy = audit._case_result(provider, case, token='fixture-token', specs=specs,
        provider_fetcher=generic, diagnostic_fetcher=direct, transport=transport, today=TODAY)
    result = audit._fidelity_case_result(provider, case, legacy, token='fixture-token', specs=specs,
        provider_fetcher=generic, diagnostic_fetcher=direct, transport=transport,
        identity_context_mode='PRODUCTION_EQUIVALENT_CAPTURE', today=TODAY)
    return result, calls


RECORDED_FORENSIC_SHA256 = "e2d08504ac9e2e0996d7c02e76024bd67a93a6bc054419815baa277ca585eca8"
