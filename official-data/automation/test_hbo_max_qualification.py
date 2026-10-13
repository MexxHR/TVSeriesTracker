"""Synthetic offline safety/qualification contracts; not live-source evidence."""
import datetime as dt
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hbo_max_qualification as q
import spanish_qualification_parser as es

TODAY=dt.date(2026,10,12)
ENTRY={'tmdbId':1,'title':'SERIES A','aliases':['SERIES A'],'allowedDomains':[q.HOST]}
URL=q.ROOTS[0]+'post/series-a-renewal'

def article(text,publication='2026-03-01'):
    return {'title':'','body':text,'url':URL,'sourceName':q.SOURCE,'publicationDate':publication,'language':'es','territory':'ES'}

def html(title,text='',date='2026-03-01'):
    return f'<html><head><title>{title}</title><meta property="article:published_time" content="{date}"><link rel="canonical" href="{URL}"></head><body><h1>{title}</h1><main><p>{text}</p></main></body></html>'

def case(kind='positive',route='HBO',key='fixture'):
    return {'caseId':key,'tmdbId':1,'title':'SERIES A','aliases':['SERIES A'],'candidateRoute':route,'kind':kind,'knownUrls':[URL]}

class SpanishTests(unittest.TestCase):
    def facts(self,text,entry=ENTRY):
        return es.detect(article(text),entry,TODAY)

    def test_prompt_renewal_hbo(self):
        entry={**ENTRY,'title':'THE LAST OF US','aliases':['THE LAST OF US']}
        facts=self.facts('HBO renueva THE LAST OF US por una tercera temporada',entry)
        self.assertEqual((facts[0]['status'],facts[0]['nextSeasonNumber']),('RENEWED',3))

    def test_prompt_renewal_max(self):
        entry={**ENTRY,'title':'HACKS','aliases':['HACKS']}
        self.assertEqual(self.facts('MAX renueva HACKS por una cuarta temporada',entry)[0]['nextSeasonNumber'],4)

    def test_all_ordinals(self):
        for word,n in es.ORDINALS.items():
            with self.subTest(word=word):
                facts=self.facts(f'HBO renueva SERIES A por una {word} temporada.')
                self.assertEqual(facts[0]['nextSeasonNumber'],n)

    def test_numeric_local_seasons(self):
        for token in ('temporada 3','3ª temporada','3.ª temporada','3 temporada'):
            self.assertEqual(self.facts('HBO renueva SERIES A para una '+token)[0]['nextSeasonNumber'],3)
        self.assertEqual(self.facts('HBO renueva SERIES A para 2027.'),[])

    def test_unicode_normalization_preserves_identity_accents(self):
        text='HBO\xa0renueva “SERIES A” por una se\u0301ptima temporada'
        self.assertEqual(self.facts(text)[0]['nextSeasonNumber'],7)
        entry={**ENTRY,'title':'ÁREA','aliases':['ÁREA']}
        self.assertEqual(self.facts('HBO renueva AREA por una segunda temporada',entry),[])

    def test_passive_renewals(self):
        for verb in ('ha sido renovada','ha sido renovado','renovada','renovado','se renueva'):
            self.assertEqual(self.facts(f'SERIES A {verb} por una tercera temporada')[0]['nextSeasonNumber'],3)

    def test_generic_described_object_renewal(self):
        facts=self.facts('HBO ha renovado su nueva serie de comedia SERIES A para una segunda temporada')
        self.assertEqual(facts[0]['nextSeasonNumber'],2)
        facts=self.facts('Max renueva SERIES A, la comedia ganadora de un Emmy, por una cuarta temporada')
        self.assertEqual(facts[0]['nextSeasonNumber'],4)

    def test_final_title_before_and_after(self):
        for text in ('SERIES A vuelve con su quinta y última temporada',
                     'La quinta y última temporada de SERIES A',
                     'SERIES A regresa con su quinta temporada, que será la última'):
            facts=self.facts(text)
            self.assertEqual((facts[0]['status'],facts[0]['nextSeasonNumber']),('FINAL_SEASON',5))

    def test_renewal_for_final_season_ranks_final(self):
        self.assertEqual(self.facts('HBO renueva SERIES A por una quinta y última temporada')[0]['status'],'FINAL_SEASON')

    def test_numberless_final_stays_safe(self):
        for text in ('SERIES A vuelve con su última temporada','La temporada final de SERIES A'):
            self.assertEqual(self.facts(text),[])

    def test_filming_and_return_not_renewal(self):
        for text in ('La cuarta temporada de SERIES A comienza a rodarse.',
                     'SERIES A regresa con su tercera temporada.',
                     'El rodaje de la tercera temporada de SERIES A ha comenzado.'):
            self.assertEqual(self.facts(text),[])

    def test_negation_and_speculation(self):
        for text in ('SERIES A no ha sido renovada por una tercera temporada.',
                     'SERIES A no ha sido renovado por una tercera temporada.',
                     'SERIES A todavía no ha sido renovada.',
                     'SERIES A aún no ha sido renovada por una tercera temporada.',
                     'No se ha confirmado una tercera temporada de SERIES A.',
                     'SERIES A podría ser renovada para una tercera temporada.',
                     'SERIES A espera ser renovada para una tercera temporada.',
                     'Se habla de una posible tercera temporada de SERIES A.',
                     'No es cierto que HBO renueva SERIES A por una tercera temporada.'):
            self.assertEqual(self.facts(text),[],text)

    def test_final_episode_and_trailer_not_final_season(self):
        for text in ('El último episodio de la quinta temporada de SERIES A.',
                     'El último tráiler de la temporada de SERIES A.',
                     'El último capítulo de la quinta temporada de SERIES A.',
                     'La última entrega de SERIES A.'):
            self.assertEqual(self.facts(text),[])

    def test_teaser_not_renewal(self):
        text='HBO Max lanza el teaser oficial y las primeras imágenes de la tercera temporada de SERIES A.'
        self.assertEqual(self.facts(text),[])

    def test_cross_season_both_orders(self):
        renewal='HBO renueva SERIES A por una tercera temporada.'
        date='La segunda temporada de SERIES A se estrena el 14 de abril.'
        for text in (renewal+date,date+renewal):
            a=article(text);facts=es.detect(a,ENTRY,TODAY);dates=es.regional_dates(a,ENTRY)
            self.assertEqual(facts[0]['nextSeasonNumber'],3)
            self.assertIsNone(facts[0]['releaseDate'])
            self.assertEqual(dates[0]['nextSeasonNumber'],2)

    def test_cross_season_anonymous_date_cannot_bind(self):
        a=article('HBO renueva SERIES A por una tercera temporada. La segunda temporada se estrena el 14 de abril.')
        self.assertEqual(es.regional_dates(a,ENTRY),[])

    def test_multititle_subjects_both_orders(self):
        for text in ('HBO renueva SERIES A por una tercera temporada. SERIES B vuelve con su segunda temporada el 10 de abril.',
                     'SERIES B vuelve con su segunda temporada el 10 de abril. HBO renueva SERIES A por una tercera temporada.'):
            a=article(text)
            self.assertEqual(es.regional_dates(a,ENTRY),[])
            self.assertEqual(es.detect(a,{**ENTRY,'title':'SERIES B','aliases':['SERIES B']},TODAY),[])

    def test_multititle_final_numbers(self):
        text='La quinta y última temporada de SERIES A. La tercera temporada de SERIES B.'
        self.assertEqual(self.facts(text)[0]['nextSeasonNumber'],5)
        self.assertEqual(self.facts(text,{**ENTRY,'title':'SERIES B','aliases':['SERIES B']}),[])

    def test_partial_title_rejected(self):
        self.assertEqual(self.facts('HBO renueva SERIES ABC por una tercera temporada'),[])

    def test_regional_date_never_global(self):
        a=article('SERIES A vuelve con su quinta y última temporada el 10 de abril.')
        facts=es.detect(a,ENTRY,TODAY);dates=es.regional_dates(a,ENTRY)
        self.assertIsNone(facts[0]['releaseDate']);self.assertIsNone(facts[0]['releaseYear'])
        self.assertEqual(dates[0]['regionalReleaseDate'],'2026-04-10')
        self.assertEqual(dates[0]['territory'],'ES')

    def test_no_publication_no_invented_year(self):
        dates=es.regional_dates(article('SERIES A vuelve con su quinta temporada el 10 de abril.',None),ENTRY)
        self.assertIsNone(dates[0]['regionalReleaseDate']);self.assertIsNone(dates[0]['releaseYear'])

    def test_year_boundary_ambiguous(self):
        dates=es.regional_dates(article('SERIES A vuelve con su quinta temporada el 10 de enero.','2026-12-01'),ENTRY)
        self.assertIsNone(dates[0]['regionalReleaseDate'])

    def test_explicit_year_and_invalid_date(self):
        dates=es.regional_dates(article('SERIES A vuelve con su quinta temporada el 10 de enero de 2027.','2026-12-01'),ENTRY)
        self.assertEqual(dates[0]['regionalReleaseDate'],'2027-01-10')
        self.assertEqual(es.regional_dates(article('SERIES A vuelve con su quinta temporada el 31 de febrero de 2027.'),ENTRY),[])

    def test_month_window_not_exact(self):
        self.assertEqual(es.regional_dates(article('SERIES A vuelve con su tercera temporada en enero de 2027.'),ENTRY),[])

    def test_cancellation_not_inferred(self):
        for text in ('SERIES A no tiene planes futuros.','SERIES A ha desaparecido de la web.','El episodio final de SERIES A.'):
            self.assertEqual(self.facts(text),[])

    def test_bounded_excerpt_replay(self):
        facts=self.facts('HBO renueva SERIES A por una tercera temporada. '+'Otro contexto. '*1000)
        self.assertLessEqual(len(facts[0]['evidenceText']),200)
        self.assertEqual(self.facts(facts[0]['evidenceText']),facts)

    def test_invalid_encoding_rejected(self):
        with self.assertRaisesRegex(ValueError,'INVALID_TEXT_ENCODING'):
            self.facts('SERIES A vuelve con su quinta y �ltima temporada')

    def test_explicit_qualification_language_required(self):
        a=article('HBO renueva SERIES A por una tercera temporada');a['language']='en'
        with self.assertRaises(ValueError):
            es.detect(a,ENTRY,TODAY)

class QualificationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        for name in q.integrity.PROTECTED_FILES:
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'protected')

    def run_q(self,cases,pages,metadata=None,fetcher=None,*,github_actions=False):
        def fetch(url,domains):
            raw=pages.get(url)
            return ({'status':'fetched','httpStatus':200,'finalUrl':url},raw) if raw else ({'status':'failed','failureCategory':'DNS_FAILURE'},None)
        # Synthetic context is explicit; never inherit the physical CI host.
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'true' if github_actions else 'false'}):
            return q.run(root=self.root,config={'minimumDistinctSeriesPerRoute':3,'cases':cases},fetcher=fetcher or fetch,
                         token='synthetic',metadata_loader=lambda i,t:metadata or {'title':'SERIES A','networks':[{'name':'HBO'}]},today=TODAY)

    def test_baseline_is_actual_runner_and_checksum(self):
        baseline=q.baseline_proof()
        self.assertEqual(baseline['resultScope'],'RUNNER_CONFIRMATION')
        self.assertTrue(set(q.ROOTS)<=set(baseline['accessibleRegionalUrls']))
        self.assertEqual(baseline['artifactSha256'],q.BASELINE_SHA)
        p=self.root/'local.json';p.write_text('{"resultScope":"LOCAL_DIAGNOSTIC_ONLY"}')
        with self.assertRaisesRegex(ValueError,'checksum'):
            q.baseline_proof(p)

    def test_separate_network_routes_and_unknowns(self):
        for labels,expected in [(['HBO'],'HBO'),(['Max'],'MAX'),(['HBO Max'],'MAX'),(['Max','HBO Max'],'MAX'),
                                (['HBO','Max'],None),(['Warner Bros.'],None),(['HBO','BBC One'],None),([],None)]:
            self.assertEqual(q.audit.proposed_route({'networks':labels,'productionCompanies':['HBO'],'homepage':'https://hbo.com'})['route'],expected)

    def test_exact_host_only(self):
        for url in ('https://random-subdomain.max.com/post/a','https://max.com/post/a','https://evil.press-spain.max.com/post/a',
                    'https://press-spain.max.com.evil.test/post/a','http://press-spain.max.com/post/a','https://mirror.test/post/a'):
            self.assertFalse(q.allowed(url),url)
        self.assertTrue(q.allowed(URL))

    def test_root_and_sitemap_are_not_evidence(self):
        self.assertFalse(q.evidence_url(q.ROOTS[0]));self.assertFalse(q.evidence_url(q.ROOTS[1]))
        a=article('HBO renueva SERIES A por una tercera temporada');a['url']=q.ROOTS[1]
        with self.assertRaises(q.update.AutomationError):
            q.detector(a,ENTRY,TODAY)

    def test_charset_declared_not_repaired(self):
        text='quinta y última temporada'
        self.assertEqual(q.decode_html(text.encode('cp1252'),'windows-1252'),text)
        self.assertEqual(q.decode_html(text.encode('utf-8'),'utf-8'),text)
        self.assertEqual(q.decode_html(b'<meta charset="iso-8859-1">'+text.encode('latin1'),None),'<meta charset="iso-8859-1">'+text)
        with self.assertRaises(UnicodeError):
            q.decode_html(text.encode('cp1252'),'utf-8')
        with self.assertRaises(ValueError):
            q.decode_html(b'body','utf-7')

    def test_editorial_sidebar_and_latest_news_excluded(self):
        text=q.Editorial();text.feed('<h1>SERIES A</h1><p>Cast only.</p><nav>HBO renueva SERIES A por una tercera temporada</nav><aside>HBO renueva SERIES A por una tercera temporada</aside><h2>Últimas noticias</h2><p>HBO renueva SERIES A por una tercera temporada</p>')
        self.assertNotIn('renueva',text.text())

    def test_jsonld_published_strict_article_identity(self):
        heading='HBO renueva SERIES A por una tercera temporada'
        data={'@type':'NewsArticle','headline':heading,'url':URL,'datePublished':'2026-03-01','dateModified':'2026-04-01'}
        raw='<script type="application/ld+json">'+json.dumps(data)+'</script>'
        self.assertEqual(q.published(raw,URL,heading),'2026-03-01')
        self.assertIsNone(q.published(raw,URL,'Other Show'))
        data['@type']='Organization'
        self.assertIsNone(q.published('<script type="application/ld+json">'+json.dumps(data)+'</script>',URL,heading))

    def test_publication_conflict_fails_closed(self):
        raw='<meta property="article:published_time" content="2026-01-01"><time datetime="2026-02-01">date</time>'
        self.assertIsNone(q.published(raw,URL,'SERIES A'))

    def test_publication_conflict_not_overridden_by_jsonld(self):
        raw='<meta property="article:published_time" content="2026-01-01"><time datetime="2026-02-01">date</time>'
        data={'@type':'NewsArticle','headline':'SERIES A','url':URL,'datePublished':'2026-03-01'}
        self.assertIsNone(q.published(raw+'<script type="application/ld+json">'+json.dumps(data)+'</script>',URL,'SERIES A'))

    def test_live_body_and_index_are_required(self):
        pages={URL:html('HBO renueva SERIES A por una tercera temporada')}
        result=self.run_q([case()],pages);row=result['hbo']['cases'][0]
        self.assertFalse(row['contentPassed']);self.assertFalse(row['passed'])
        self.assertEqual(row['discoveryMethod'],'NO_CANDIDATE')
        self.assertEqual(row['independentReconstruction']['result'],'not_applicable')
        pages[q.ROOTS[1]]='<a href="'+URL+'">SERIES A</a>'
        row=self.run_q([case()],pages)['hbo']['cases'][0]
        self.assertTrue(row['passed']);self.assertEqual(row['discoveryMethod'],'DISCOVERED_FROM_FIRST_PARTY_INDEX')

    def test_wrong_show_200_rejected(self):
        result=self.run_q([case()],{q.ROOTS[1]:'<a href="'+URL+'">SERIES A</a>',URL:html('Other Show','HBO renueva SERIES A por una tercera temporada')})
        self.assertFalse(result['hbo']['cases'][0]['passed'])
        self.assertEqual(result['hbo']['cases'][0]['failureStage'],'identity')

    def test_source_brand_not_inferred_from_domain(self):
        result=self.run_q([case()],{q.ROOTS[1]:'<a href="'+URL+'">SERIES A</a>',URL:html('Max renueva SERIES A por una tercera temporada')})
        self.assertFalse(result['hbo']['cases'][0]['providerIdentityMatched'])

    def test_current_networks_required_not_historical_baseline(self):
        pages={q.ROOTS[1]:'<a href="'+URL+'">SERIES A</a>',URL:html('HBO renueva SERIES A por una tercera temporada')}
        result=self.run_q([case()],pages,metadata={'title':'SERIES A','networks':[],'productionCompanies':['HBO'],'homepage':'https://hbo.com'})
        self.assertIsNone(result['hbo']['cases'][0]['route'])
        self.assertFalse(result['hbo']['cases'][0]['contentPassed'])

    def test_wbd403_safe_one_probe_no_retry(self):
        calls=[]
        def fetch(url,domains):
            calls.append(url)
            return {'status':'blocked','httpStatus':403,'failureCategory':'HTTP_403'},None
        result=self.run_q([],{},fetcher=fetch)
        self.assertTrue(result['wbd403']['observed']);self.assertFalse(result['wbd403']['bypassAttempted'])
        self.assertEqual(sum('press.wbd.com' in u for u in calls),1)

    def test_foreign_redirect_body_is_not_evidence(self):
        def fetch(url,domains):
            return {'status':'fetched','httpStatus':200,'finalUrl':'https://evil.test/post/series-a'},html('HBO renueva SERIES A por una tercera temporada')
        row=self.run_q([case()],{},fetcher=fetch)['hbo']['cases'][0]
        self.assertFalse(row['contentPassed']);self.assertEqual(row['parserFacts'],[])

    def test_real_negative_requires_fetched_identity(self):
        row=self.run_q([case('negative')],{URL:html('SERIES A comienza el rodaje de su tercera temporada')})['hbo']['cases'][0]
        # Without original-brand corroboration this is not a provider-negative PASS.
        self.assertFalse(row['passed'])
        row=self.run_q([case('negative')],{URL:html('La tercera temporada de la serie original de HBO SERIES A comienza a rodarse')})['hbo']['cases'][0]
        self.assertTrue(row['passed']);self.assertEqual(row['parserFacts'],[])
        self.assertFalse(self.run_q([case('negative')],{})['hbo']['cases'][0]['passed'])

    def test_independent_fresh_and_excerpt_replay_fields(self):
        a=article('SERIES A vuelve con su quinta y última temporada el 10 de abril.')
        facts=es.detect(a,ENTRY,TODAY);dates=es.regional_dates(a,ENTRY)
        merged,_=q.update.merge(None,facts,TODAY)
        expected={k:merged.get(k) for k in q.promotion.monitored.FACT_FIELDS}
        result=q.reconstruction(facts,dates,html('SERIES A vuelve con su quinta y última temporada el 10 de abril.'),URL,ENTRY,TODAY,expected)
        self.assertEqual(result['result'],'passed',result)
        self.assertEqual(len(result['fieldsCompared']),7);self.assertEqual(result['mismatchedFields'],[])
        self.assertIsNone(result['reconstructedFacts']['releaseDate'])

    def test_tampered_expected_field_mismatch(self):
        a=article('HBO renueva SERIES A por una tercera temporada');facts=es.detect(a,ENTRY,TODAY)
        merged,_=q.update.merge(None,facts,TODAY);expected={k:merged.get(k) for k in q.promotion.monitored.FACT_FIELDS}
        expected['nextSeasonNumber']=4
        result=q.reconstruction(facts,[],html(a['body']),URL,ENTRY,TODAY,expected)
        self.assertEqual(result['result'],'mismatch');self.assertIn('nextSeasonNumber',result['mismatchedFields'])

    def test_persisted_evidence_mutation_rejected(self):
        a=article('HBO renueva SERIES A por una tercera temporada');facts=es.detect(a,ENTRY,TODAY)
        evidence=[{**{k:facts[0].get(k) for k in q.promotion.monitored.EVIDENCE_FIELDS},'publicationDateSource':'structured_metadata'}]
        evidence[0]['evidenceText']='SERIES A no ha sido renovada por una tercera temporada'
        row={'tmdbId':1,'title':'SERIES A','lastChecked':TODAY.isoformat(),'parserResult':'facts','validationResult':'passed','eligibleSourceUrls':[URL],'sourceEvidence':evidence}
        with self.assertRaises(q.update.AutomationError):
            q.promotion._validate_evidence(row,[q.HOST],detector=q.detector)

    def test_three_distinct_series_no_duplicate_inflation(self):
        safety={'fixtures':{'passed':True}}
        row={'tmdbId':1,'kind':'positive','contentPassed':True,'passed':True,'discoveryMethod':'DISCOVERED_FROM_FIRST_PARTY_INDEX','independentReconstruction':{'result':'passed'}}
        result=q.qualification_state([row,row,row],safety,True,True)
        self.assertEqual(result['distinctPositiveSeries'],1)
        self.assertNotEqual(result['qualificationState'],'QUALIFIED_ON_GITHUB_RUNNER')
        rows=[{**row,'tmdbId':i} for i in (1,2,3)]
        self.assertEqual(q.qualification_state(rows,safety,True,True)['qualificationState'],'QUALIFIED_ON_GITHUB_RUNNER')
        self.assertNotEqual(q.qualification_state(rows,{'bad':{'passed':False}},True,True)['qualificationState'],'QUALIFIED_ON_GITHUB_RUNNER')
        self.assertEqual(q.qualification_state(rows,safety,False,True)['qualificationState'],'LOCAL_DIAGNOSTIC_PASS_PENDING_RUNNER')

    def test_threshold_cannot_be_lowered(self):
        with self.assertRaises(ValueError):
            q.run(root=self.root,config={'minimumDistinctSeriesPerRoute':2,'cases':[]})

    def test_hbo_max_independent_thresholds(self):
        safety={'fixtures':{'passed':True}}
        row={'kind':'positive','contentPassed':True,'passed':True,'discoveryMethod':'DISCOVERED_FROM_FIRST_PARTY_INDEX','independentReconstruction':{'result':'passed'}}
        self.assertEqual(q.qualification_state([{**row,'tmdbId':i} for i in (1,2,3)],safety,True,True)['qualificationState'],'QUALIFIED_ON_GITHUB_RUNNER')
        self.assertEqual(q.qualification_state([{**row,'tmdbId':4}],safety,True,True)['qualificationState'],'INSUFFICIENT_DISTINCT_FIRST_PARTY_CASES')

    def test_complete_synthetic_runner_qualified_activation_stays_false(self):
        cases=[];pages={};links=[];names={}
        for i in range(1,7):
            name='SERIES '+chr(64+i);names[i]=name
            route='HBO' if i<=3 else 'MAX';url=q.ROOTS[0]+'post/series-'+chr(96+i)
            title=f'{route} renueva {name} por una tercera temporada'
            pages[url]=html(title).replace(URL,url)
            cases.append({'caseId':str(i),'tmdbId':i,'title':name,'aliases':[name],'kind':'positive','candidateRoute':route,'knownUrls':[]})
            links.append(f'<a href="{url}">{name}</a>')
        for i in (1,4):
            name=names[i];route='HBO' if i==1 else 'MAX';url=q.ROOTS[0]+f'post/negative-{i}'
            pages[url]=html(f'La tercera temporada de la serie original de {route} {name} comienza a rodarse').replace(URL,url)
            cases.append({'caseId':'negative'+str(i),'tmdbId':i,'title':name,'aliases':[name],'kind':'negative','candidateRoute':route,'knownUrls':[url]})
        url=q.ROOTS[0]+'post/monthly';pages[url]=html('Monthly','SERIES A estrena su segunda temporada. SERIES B tiene noticias.').replace(URL,url)
        cases.append({'caseId':'monthly','tmdbId':1,'title':names[1],'aliases':[names[1]],'kind':'ambiguity','candidateRoute':'HBO','knownUrls':[url]})
        pages[q.ROOTS[1]]=''.join(links)
        def fetch(url,domains):
            raw=pages.get(url)
            return ({'status':'fetched','httpStatus':200,'finalUrl':url},raw) if raw else ({'status':'failed'},None)
        def metadata(i,token):
            return {'title':names[i],'networks':[{'name':'HBO' if i<=3 else 'Max'}]}
        with patch.object(q.http,'_is_github_actions',return_value=True):
            result=q.run(root=self.root,config={'minimumDistinctSeriesPerRoute':3,'cases':cases},fetcher=fetch,token='synthetic',metadata_loader=metadata,today=TODAY)
        for key in ('hbo','max'):
            self.assertEqual(result[key]['qualificationState'],'QUALIFIED_ON_GITHUB_RUNNER',result[key])
            self.assertEqual(result[key]['genericDiscoveryPassingSeries'],3)
        self.assertEqual(result['productionActivation'],{'HBO':False,'MAX':False})

    def test_schema_no_writes_and_local_inconclusive(self):
        for host in ('true','false'):
            with self.subTest(host=host),patch.dict(os.environ,{'GITHUB_ACTIONS':host}):
                result=self.run_q([case()],{})
                self.assertEqual(os.environ['GITHUB_ACTIONS'],host)
                self.assertEqual(result['resultScope'],'LOCAL_DIAGNOSTIC_ONLY')
                for route in ('hbo','max'):
                    self.assertEqual(result[route]['qualificationState'],'LOCAL_NETWORK_INCONCLUSIVE')
        self.assertTrue(result['qualificationOnly'] and result['readOnly'])
        self.assertEqual(result['hbo']['qualificationState'],'LOCAL_NETWORK_INCONCLUSIVE')
        self.assertEqual(result['productionActivation'],{'HBO':False,'MAX':False})
        self.assertEqual(result['protectedFileIntegrity']['changedFiles'],[])
        self.assertTrue(result['protectedFileIntegrity']['unchanged'])
        json.dumps(result)

    def test_runner_access_failure_context_is_explicit(self):
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}):
            result=self.run_q([case()],{},github_actions=True)
            self.assertEqual(os.environ['GITHUB_ACTIONS'],'false')
        self.assertEqual(result['resultScope'],'RUNNER_CONFIRMATION')
        for route in ('hbo','max'):
            self.assertEqual(result[route]['qualificationState'],'RUNNER_ACCESS_NEEDS_WORK')
        self.assertEqual(result['productionActivation'],{'HBO':False,'MAX':False})
        self.assertEqual(result['protectedFileIntegrity']['changedFiles'],[])
        self.assertTrue(result['protectedFileIntegrity']['unchanged'])

    def test_normal_nonqualified_run_writes_real_result_artifact(self):
        result=self.run_q([case()],{},github_actions=True)
        output=self.root/'hbo-max-provider-qualification.json'
        with patch.object(sys,'path',sys.path+[str(q.update.ROOT/'backend')]),patch.object(q.update,'ROOT',self.root),patch.object(q,'run',return_value=result),patch('sys.argv',['qualification','--output',str(output)]):
            q.main()
        self.assertEqual(json.loads(output.read_text(encoding='utf-8')),result)
        self.assertEqual(result['hbo']['qualificationState'],'RUNNER_ACCESS_NEEDS_WORK')

    def test_protected_mutation_aborts(self):
        def fetch(url,domains):
            (self.root/q.integrity.PROTECTED_FILES[0]).write_bytes(b'changed')
            return {'status':'failed'},None
        with self.assertRaisesRegex(RuntimeError,'Protected files changed'):
            self.run_q([],{},fetcher=fetch)

    def test_shared_safety_executes(self):
        result=q.shared_safety()
        self.assertTrue(all(v['passed'] for v in result.values()))
        self.assertEqual(set(result),{'multiTitle','crossSeason','negation','teaserTrailer','regionalReleaseDate'})

    def test_existing_english_parser_does_not_activate_spanish(self):
        a=article('HBO renueva SERIES A por una tercera temporada')
        self.assertEqual(q.update.detect(a,ENTRY,TODAY,strict_binding=True),[])

    def test_workflow_read_only_contract(self):
        root=Path(__file__).resolve().parents[2]
        workflow=(root/'.github/workflows/qualify-hbo-max.yml').read_text()
        for needed in ('name: HBO MAX Provider Qualification','workflow_dispatch:','contents: read','persist-credentials: false',
                       'sha256sum -c','git diff --exit-code HEAD','hbo_max_qualification.py','name: hbo-max-provider-qualification'):
            self.assertIn(needed,workflow)
        for forbidden in ('contents: write','schedule:','git push','git commit','promotion.py','monitored.py','update.py','assembleDebug','package_release.py'):
            self.assertNotIn(forbidden,workflow)
        self.assertEqual(workflow.count('secrets.'),1)
        ordered=('actions/checkout@','actions/setup-python@','name: Snapshot protected files',
                 'name: Spanish and qualification safety tests','name: Real runner qualification',
                 'name: Verify protected integrity','name: Check qualification artifact','uses: actions/upload-artifact@')
        positions=[workflow.index(s) for s in ordered]
        self.assertEqual(positions,sorted(positions))
        self.assertIn('steps.qualification_artifact.outputs.generated',workflow)
        self.assertIn('::error::Qualification artifact was not generated',workflow)
        real_step=workflow.split('- name: Real runner qualification',1)[1].split('- name: Verify protected integrity',1)[0]
        self.assertNotIn('GITHUB_ACTIONS',real_step)

if __name__=='__main__':
    unittest.main()
