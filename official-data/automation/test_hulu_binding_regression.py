"""V2.7.3.2 quoted identity binding; synthetic variants are not fetched evidence."""
import datetime as dt
import unittest

import update
import fx_hulu_qualification as qualification
from test_shrill_reconstruction import SHRILL_HTML, SHRILL_URL

TODAY = dt.date(2026, 10, 11)
DELI_URL = ('https://press.hulu.com/pressrelease/'
            'hulus-critically-acclaimed-comedy-series-deli-boys-renewed-for-a-second-season/')


def detect(text, title):
    return update.detect({'title': text, 'body': '', 'url': DELI_URL,
                          'sourceName': 'Hulu Press'},
                         {'tmdbId': 1, 'title': title, 'aliases': [title]},
                         TODAY, strict_binding=True)


class QuotedBindingTests(unittest.TestCase):
    def test_quoted_body_subject_keeps_explicit_passive_renewal_binding(self):
        for left, right in [("'", "'"), ('"', '"'), ('\u2018', '\u2019'), ('\u201c', '\u201d')]:
            text = f'The comedy series {left}Deli Boys{right} has been renewed for a second season'
            self.assertEqual([(f['status'], f['nextSeasonNumber']) for f in detect(text, 'Deli Boys')],
                             [('RENEWED', 2)])

    def test_runner_deli_headline_and_quote_variants_replay_all_seven_fields(self):
        for left, right in [("'", "'"), ('"', '"'), ('\u2018', '\u2019'), ('\u201c', '\u201d')]:
            for apostrophe in ["'", '\u2019']:
                heading = (f'HULU{apostrophe}S CRITICALLY ACCLAIMED COMEDY SERIES '
                           f'{left}DELI BOYS{right} RENEWED FOR A SECOND SEASON')
                with self.subTest(heading=heading):
                    self.assertEqual([(f['status'], f['nextSeasonNumber'])
                                      for f in detect(heading, 'Deli Boys')], [('RENEWED', 2)])
                    raw = f'<html><title>{heading}</title><h1>{heading}</h1></html>'
                    result = qualification._fact_reconstruction(
                        {'series': 'Deli Boys', 'aliases': ['Deli Boys'], 'discoveredUrl': DELI_URL},
                        1, ['press.hulu.com'], TODAY,
                        lambda u, d: ({'status': 'fetched', 'finalUrl': DELI_URL}, raw))
                    self.assertEqual(result['result'], 'passed')
                    self.assertEqual(result['mismatchedFields'], [])
                    self.assertEqual(len(result['fieldsCompared']), 7)

    def test_shrill_typographic_double_quotes_and_long_multititle_excerpt_replay(self):
        # Preserve the existing article fixture's wording and season expectations.
        raw = SHRILL_HTML.replace('"Shrill"', '\u201cShrill\u201d')
        result = qualification._fact_reconstruction(
            {'series': 'Shrill', 'aliases': ['Shrill'], 'discoveredUrl': SHRILL_URL},
            1, ['press.hulu.com'], TODAY,
            lambda u, d: ({'status': 'fetched', 'finalUrl': SHRILL_URL}, raw))
        self.assertEqual(result['result'], 'passed')
        self.assertEqual(result['mismatchedFields'], [])
        self.assertEqual(len(result['fieldsCompared']), 7)
        self.assertEqual((result['verifiedFacts']['status'],
                          result['verifiedFacts']['nextSeasonNumber']), ('FINAL_SEASON', 3))
        for evidence in result['sourceEvidence']:
            self.assertLessEqual(len(evidence['evidenceText']), 200)
            self.assertIn('Shrill', evidence['evidenceText'])

    def test_missing_or_different_or_partial_identity_fails(self):
        for text in ['A comedy renewed for a second season',
                     '“Another Show” renewed for a second season',
                     '“Deli Boys Extended” renewed for a second season',
                     '“SuperDeli Boys” renewed for a second season',
                     'Deli Boys discusses “Another Show” renewed for a second season',
                     'Deli Boys cast announced for season two',
                     'Deli Boys season two premieres soon']:
            with self.subTest(text=text):
                self.assertFalse(detect(text, 'Deli Boys'))

    def test_multititle_seasons_and_statuses_are_not_transferred(self):
        cases = [
            ('Series A returns for season four, while the final season of Series B premieres later.',
             {'Series A': [], 'Series B': []}),
            ('Series A has been renewed for season three alongside Series B, which concludes with its fourth and final season.',
             {'Series A': [('RENEWED', 3)], 'Series B': []}),
            ('Series A, Series B and Series C return next year.',
             {'Series A': [], 'Series B': [], 'Series C': []}),
            ('The fourth season of “Series A”, the third and final season of comedy series “Series B” and “Series C” join the slate.',
             {'Series A': [], 'Series B': [('FINAL_SEASON', 3)], 'Series C': []}),
        ]
        for text, expected in cases:
            for title, facts in expected.items():
                with self.subTest(text=text, title=title):
                    self.assertEqual([(f['status'], f['nextSeasonNumber'])
                                      for f in detect(text, title)], facts)

    def test_negation_speculation_and_promotion_remain_insufficient(self):
        for wording in ['not renewed for season two', 'has not been renewed for season two',
                        'could return for season two', 'may return for season two',
                        'expected to return for season two', 'reportedly renewed for season two',
                        'cast joins season two', 'season two premieres soon']:
            for quote in ['', '"']:
                with self.subTest(wording=wording, quote=quote):
                    self.assertFalse(detect(f'{quote}Deli Boys{quote} {wording}', 'Deli Boys'))


if __name__ == '__main__':
    unittest.main()
