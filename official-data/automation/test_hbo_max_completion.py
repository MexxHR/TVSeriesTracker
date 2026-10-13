"""MAX-only punctuation/replay regressions; no live qualification proof."""
import copy
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import hbo_max_qualification as q
import max_qualification_parser as parser
import hbo_production
import test_hbo_max_qualification as fixtures

TODAY=dt.date(2026,10,13)
ENTRY={'tmdbId':116450,'title':'And Just Like That...','aliases':['And Just Like That...'],'qualificationRoute':'MAX'}
URL='https://press-spain.max.com/post/hbo-max-renueva-su-serie-and-just-like-that-por-una-segunda-temp'


def article(body,title=''):
    return {'title':title,'body':body,'url':URL,'sourceName':q.SOURCE,'language':'es','territory':'ES','publicationDate':'2022-03-22'}


class MaxCompletionTests(unittest.TestCase):
    setUp=fixtures.QualificationTests.setUp
    run_q=fixtures.QualificationTests.run_q

    def test_actual_first_party_passive_claim_was_split_at_title_ellipsis(self):
        a=article('AND JUST LIKE THAT... ha sido renovada por una segunda temporada')
        self.assertEqual(q.spanish.detect(a,ENTRY,TODAY),[])
        facts=parser.detect(a,ENTRY,TODAY)
        self.assertEqual((facts[0]['status'],facts[0]['nextSeasonNumber']),('RENEWED',2))
        self.assertEqual(facts[0]['title'],ENTRY['title'])
        self.assertIsNone(facts[0]['releaseDate']);self.assertIsNone(facts[0]['releaseYear'])

    def test_generic_unrelated_titles_and_unicode_ellipsis(self):
        for name in ('SERIES A...','OTHER SHOW…'):
            entry={**ENTRY,'title':name,'aliases':[name]}
            facts=parser.detect(article(name+' ha sido renovada por una tercera temporada'),entry,TODAY)
            self.assertEqual(facts[0]['nextSeasonNumber'],3)
            self.assertEqual(facts[0]['title'],name)

    def test_only_exact_alias_ellipsis_is_normalized(self):
        a=article('SERIES A... ha sido renovada por una tercera temporada. OTHER... unrelated.')
        normalized,_=parser.scoped(a,{**ENTRY,'title':'SERIES A...','aliases':['SERIES A...']})
        self.assertIn('OTHER...',normalized['body']);self.assertNotIn('SERIES A...',normalized['body'])

    def test_negation_speculation_and_no_inference(self):
        for body in ('AND JUST LIKE THAT... no ha sido renovada por una segunda temporada',
                     'AND JUST LIKE THAT... todavía no ha sido renovada por una segunda temporada',
                     'AND JUST LIKE THAT... podría ser renovada por una segunda temporada',
                     'AND JUST LIKE THAT... espera ser renovada por una segunda temporada',
                     'AND JUST LIKE THAT... vuelve con su segunda temporada',
                     'AND JUST LIKE THAT... presenta el teaser de su segunda temporada',
                     'La segunda temporada de AND JUST LIKE THAT... comienza su rodaje',
                     'El último episodio de AND JUST LIKE THAT...'):
            self.assertEqual(parser.detect(article(body),ENTRY,TODAY),[],body)

    def test_cross_title_similar_title_and_season_isolation(self):
        for body in ('OTHER SHOW... ha sido renovada por una segunda temporada',
                     'AND JUST LIKE THAT... ORIGINS ha sido renovada por una segunda temporada'):
            self.assertEqual(parser.detect(article(body),ENTRY,TODAY),[])
        body='AND JUST LIKE THAT... ha sido renovada por una tercera temporada. La segunda temporada se estrena el 14 de abril.'
        facts=parser.detect(article(body),ENTRY,TODAY)
        self.assertEqual(facts[0]['nextSeasonNumber'],3);self.assertIsNone(facts[0]['releaseDate'])
        self.assertEqual(parser.regional_dates(article(body),ENTRY),[])

    def test_invalid_text_encoding_still_rejected(self):
        with self.assertRaises(ValueError):
            parser.detect(article('AND JUST LIKE THAT... ha sido renovada por una �ltima temporada'),ENTRY,TODAY)

    def test_seven_field_reconstruction_with_normalized_bounded_excerpt(self):
        a=article('AND JUST LIKE THAT... ha sido renovada por una segunda temporada')
        facts=q.detector(a,ENTRY,TODAY);merged,_=q.update.merge(None,facts,TODAY)
        raw='<html><head><link rel="canonical" href="'+URL+'"><meta property="article:published_time" content="2022-03-22"></head><body><h1>HBO Max renueva su serie AND JUST LIKE THAT..., por una segunda temporada</h1><p>'+a['body']+'</p></body></html>'
        expected={k:merged.get(k) for k in q.promotion.monitored.FACT_FIELDS}
        replay=q.reconstruction(facts,[],raw,URL,ENTRY,TODAY,expected)
        self.assertEqual(replay['result'],'passed',replay);self.assertEqual(replay['mismatchedFields'],[])
        self.assertEqual(len(replay['fieldsCompared']),7)
        self.assertTrue(all(len(e['evidenceText'])<=200 for e in replay['boundedSourceEvidence']))
        expected['nextSeasonNumber']=9
        self.assertEqual(q.reconstruction(facts,[],raw,URL,ENTRY,TODAY,expected)['result'],'mismatch')

    def test_generic_discovery_required_even_when_source_oracle_exists(self):
        c={'caseId':'ellipsis','tmdbId':116450,'title':ENTRY['title'],'aliases':ENTRY['aliases'],'candidateRoute':'MAX','kind':'positive','knownUrlOracle':[URL]}
        raw='<h1>HBO Max renueva su serie AND JUST LIKE THAT..., por una segunda temporada</h1><p>AND JUST LIKE THAT... ha sido renovada por una segunda temporada</p>'
        metadata={'title':ENTRY['title'],'networks':[{'name':'Max'}]}
        result=self.run_q([c],{URL:raw},metadata=metadata)
        self.assertFalse(result['max']['cases'][0]['passed'])
        result=self.run_q([c],{q.ROOTS[1]:'<a href="'+URL+'">And Just Like That...</a>',URL:raw},metadata=metadata)
        self.assertTrue(result['max']['cases'][0]['passed'],result['max']['cases'][0])
        self.assertFalse(result['max']['cases'][0]['knownUrlUsedAsSeed'])

    def test_mixed_conan_style_route_still_ambiguous(self):
        c={'caseId':'mixed','tmdbId':116450,'title':ENTRY['title'],'aliases':ENTRY['aliases'],'candidateRoute':'MAX','kind':'positive','knownUrlOracle':[]}
        result=self.run_q([c],{},metadata={'title':ENTRY['title'],'networks':[{'name':'HBO'},{'name':'Max'}]})
        self.assertEqual(result['max']['cases'][0]['failureStage'],'routing')
        self.assertEqual(result['max']['cases'][0]['failureReason'],'AMBIGUOUS_NETWORKS')
        self.assertEqual(result['max']['distinctPositiveSeries'],0)

    def test_hbo_production_keeps_frozen_parser_path(self):
        a=article('AND JUST LIKE THAT... ha sido renovada por una segunda temporada')
        unflagged={k:v for k,v in ENTRY.items() if k!='qualificationRoute'}
        self.assertEqual(q.detector(a,unflagged,TODAY),[])
        self.assertEqual(hbo_production.detect(a,unflagged,TODAY),[])
        self.assertEqual(q.original_brand(article('', 'HBO Max renueva su serie AND JUST LIKE THAT...'),unflagged),[])
        self.assertEqual(q.original_brand(article('', 'HBO Max renueva su serie AND JUST LIKE THAT...'),ENTRY),['MAX'])

    def test_hacks_date_and_pitt_negative_unchanged(self):
        hacks={**ENTRY,'title':'Hacks','aliases':['Hacks']}
        a=article('Hacks vuelve con su quinta y última temporada el 10 de abril de 2026')
        facts=parser.detect(a,hacks,TODAY);dates=parser.regional_dates(a,hacks)
        self.assertIsNone(facts[0]['releaseDate']);self.assertIsNone(facts[0]['releaseYear'])
        self.assertEqual(dates[0]['regionalReleaseDate'],'2026-04-10')
        pitt={**ENTRY,'title':'The Pitt','aliases':['The Pitt']}
        self.assertEqual(parser.detect(article('HBO Max lanza el teaser de la tercera temporada de The Pitt'),pitt,TODAY),[])

    def test_summary_reports_observed_counts_without_synthetic_success(self):
        result=self.run_q([],{})
        summary=result['maxDiscoverySummary']
        for field in ('indexLinksExtracted','acceptedArticleCandidates','maxRelatedCandidates','maxCandidatesFetched','identityAccepted','lifecycleFactCandidates','reconstructionPasses'):
            self.assertEqual(summary[field],0)
        self.assertEqual(summary['distinctPassingMaxSeries'],[])
        self.assertFalse(result['productionActivation']['MAX'])

    def test_real_v279_baseline_not_older_local_or_runner(self):
        self.assertEqual(q.baseline_proof()['artifactSha256'],'a406dc080422a4fabcd483ca664e830beaec271bb9f71dbc0464208188c3dc9a')
        self.assertEqual(q.baseline_proof()['previousQualification']['max']['distinctPositiveSeries'],2)
        self.assertEqual(q.baseline_proof()['previousQualification']['hbo']['distinctPositiveSeries'],5)


if __name__=='__main__':unittest.main()
