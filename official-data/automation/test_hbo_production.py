"""Offline production integration; runner excerpts in synthetic HTML containers.

No live request or live write. Promotion calls below are fixture-only dry runs.
"""
import copy
import datetime as dt
import hashlib
from html import escape
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import automatic_promotion
import discovery
import hbo_production as hbo
import monitored
import promotion
import update

FIXTURES=Path(__file__).parent/'fixtures'
CORPUS=json.loads((FIXTURES/'v279_hbo_production_evidence.json').read_text(encoding='utf-8'))
NOW=dt.datetime(2026,10,13,20,tzinfo=dt.timezone.utc)
PROTECTED=('official-data/official_series_data.json','official-data/history/changes.jsonl','official-data/sources.json','official-data/monitored_series.json','official-data/history/monitored_changes.jsonl')


def document(heading,body,url,date='2026-04-09'):
    meta=f'<meta property="article:published_time" content="{date}">' if date else ''
    return '<html><head>'+meta+f'<link rel="canonical" href="{escape(url)}"></head><body><h1>{escape(heading)}</h1><main><p>{escape(body)}</p></main></body></html>'


class HboProductionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.stage=self.root/'monitored.json';self.audit=self.root/'monitored.jsonl'
        self.data=self.root/'canonical.json';self.history=self.root/'changes.jsonl';self.registry=self.root/'sources.json'
        self.data.write_bytes((FIXTURES/'v235_canonical.json').read_bytes());self.history.write_bytes(b'')
        self.registry.write_bytes(update.REGISTRY.read_bytes());self.trusted=json.loads(self.registry.read_bytes())
        self.specs=discovery.provider_specs()
        before={p:hashlib.sha256((update.ROOT/p).read_bytes()).hexdigest() for p in PROTECTED}
        self.addCleanup(lambda:self.assertEqual(before,{p:hashlib.sha256((update.ROOT/p).read_bytes()).hexdigest() for p in PROTECTED}))

    def metadata(self,c=None,**kw):
        c=c or CORPUS['positives'][1]
        return {'tmdbId':c['tmdbId'],'title':c['requestedTitle'],'originalName':c['requestedTitle'],
                'hboSpanishTitle':c['aliases'][-1],'networks':[{'name':'HBO'}],**kw}

    def pages(self,c=None):
        c=c or CORPUS['positives'][1]
        return {hbo.generic.ENTRY_POINTS[1]:f'<h1>Mapa del sitio</h1><nav><a href="{c["sourceUrl"]}">{escape(c["identityResult"]["articleHeading"])}</a></nav>',
                c['sourceUrl']:document(c['identityResult']['articleHeading'],'\n'.join(f['evidenceText'] for f in c['parserFacts']),c['sourceUrl'],c['announcementDate'])}

    def process(self,c=None,*,meta=None,pages=None,dry=False,discoverer=None):
        c=c or CORPUS['positives'][1];meta=meta or self.metadata(c);pages=self.pages(c) if pages is None else pages;calls=[]
        def fetch(url,domains):
            self.assertEqual(domains,[hbo.HOST]);calls.append(url)
            value=pages.get(url,'<h1>Index</h1>')
            if isinstance(value,Exception):raise value
            return value if isinstance(value,tuple) else (value,url)
        discoverer=discoverer or (lambda m,r:discovery.discover(m,r,fetcher=fetch))
        result=monitored.process(meta['tmdbId'],dry_run=dry,metadata=meta,discoverer=discoverer,fetcher=fetch,
            now=NOW,today=NOW.date(),registry_path=self.stage,audit_path=self.audit)
        return result,calls

    def promote(self,identifier):
        before=(self.data.read_bytes(),self.history.read_bytes())
        result=promotion.promote(identifier,dry_run=True,now=NOW,production_data=self.data,production_audit=self.history,
            production_registry=self.registry,monitored_registry=self.stage)
        self.assertEqual(before,(self.data.read_bytes(),self.history.read_bytes()))
        return result

    def test_hbo_and_max_enabled_fx_inactive(self):
        self.assertTrue(self.specs['HBO']['productionActivated']);self.assertIn('HBO',update.ADAPTERS)
        self.assertTrue(self.specs['MAX']['productionActivated']);self.assertIn('MAX',update.ADAPTERS)
        self.assertNotIn('FX',self.specs);self.assertNotIn('FX',update.ADAPTERS)
        self.assertEqual(self.specs['HBO']['officialDomains'],[hbo.HOST]);self.assertFalse(self.specs['HBO']['wildcardTrust'])

    def test_strict_hbo_route_only_and_ambiguity(self):
        for labels,expected in [(['HBO'],'HBO'),([' hBo '],'HBO'),(['Max'],'MAX'),(['HBO Max'],'MAX'),(['HBO','Max'],None),(['HBO','BBC One'],None)]:
            with self.subTest(labels=labels):
                route,_,reason=discovery.route_provider(self.metadata(networks=[{'name':n} for n in labels]),self.specs)
                self.assertEqual(route,expected)
                if 'HBO' in labels and len(labels)>1:self.assertEqual(reason,'ambiguous_provider')

    def test_company_homepage_distributor_source_cannot_route_hbo(self):
        for fields in ({'productionCompanies':[{'name':'HBO'}]}, {'productionCompanies':[{'name':'Warner Bros. Discovery'}]},
                       {'homepage':'https://hbo.com/series/a'},{'homepage':'https://press-spain.max.com/'},
                       {'owner':'WBD','studio':'HBO','distributor':'HBO','sourceName':'Max Press Spain'}):
            route=discovery.route_provider(self.metadata(networks=[],**fields),self.specs)[0]
            self.assertNotEqual(route,'HBO')

    def test_activation_config_can_disable_hbo_without_wbd_fallback(self):
        specs=copy.deepcopy(self.specs);specs['HBO']['productionActivated']=False
        self.assertIsNone(discovery.route_provider(self.metadata(),specs)[0])

    def test_other_provider_routes_unchanged(self):
        old={k:v for k,v in self.specs.items() if k!='HBO'}
        for network in ('AMC','Hulu','Disney+','Netflix','Paramount+','Apple TV','Prime Video','Warner Bros. Discovery','FX'):
            meta=self.metadata(networks=[{'name':network}])
            self.assertEqual(discovery.route_provider(meta,self.specs),discovery.route_provider(meta,old))

    def test_all_five_actual_runner_series_through_shared_production_pipeline(self):
        self.assertEqual(len(CORPUS['positives']),5)
        self.assertEqual(CORPUS['baselineArtifactSha256'],'a406dc080422a4fabcd483ca664e830beaec271bb9f71dbc0464208188c3dc9a')
        for c in CORPUS['positives']:
            with self.subTest(title=c['requestedTitle']):
                result,calls=self.process(c)
                self.assertEqual(result['pipelineState'],'VERIFIED_FACTS',result)
                self.assertEqual(result['provider'],'HBO')
                self.assertEqual(result['verifiedFacts'],{k:c[k] for k in monitored.FACT_FIELDS})
                self.assertEqual(result['candidateDiagnostics'][0]['discoverySource'],c['discoverySource'])
                self.assertFalse(result['candidateDiagnostics'][0]['knownUrlUsedAsSeed'])
                self.assertGreaterEqual(calls.count(c['sourceUrl']),2)
                row=next(r for r in json.loads(self.stage.read_bytes())['series'] if r['tmdbId']==c['tmdbId'])
                facts=promotion._validate_evidence(row,[hbo.HOST]);summary=promotion._facts_summary(facts,row,NOW.date())
                self.assertEqual({k:summary.get(k) for k in monitored.FACT_FIELDS},result['verifiedFacts'])
                self.assertTrue(self.promote(c['tmdbId'])['promotionEligible'])

    def test_known_oracle_url_is_never_runtime_seed(self):
        c=CORPUS['positives'][1];result,calls=self.process(c,pages={c['sourceUrl']:self.pages(c)[c['sourceUrl']]},dry=True)
        self.assertNotIn(c['sourceUrl'],calls);self.assertNotEqual(result['pipelineState'],'VERIFIED_FACTS')

    def test_max_route_stays_separate_and_mixed_routes_do_not_fetch(self):
        for labels in (['Max'],['HBO Max']):
            result,calls=self.process(meta=self.metadata(networks=[{'name':n} for n in labels]),dry=True)
            self.assertEqual(result['provider'],'MAX')
            self.assertNotEqual(result['pipelineState'],'VERIFIED_FACTS')
            self.assertTrue(all('press-spain.max.com' in url for url in calls))
        for labels in (['HBO','Max'],['HBO','Netflix']):
            result,calls=self.process(meta=self.metadata(networks=[{'name':n} for n in labels]),dry=True)
            self.assertNotEqual(result['pipelineState'],'VERIFIED_FACTS');self.assertEqual(calls,[])

    def test_max_original_and_hacks_cannot_be_hbo_facts(self):
        c=CORPUS['maxRegional'][0];pages=self.pages(c)
        result,_=self.process(c,meta=self.metadata(c),pages=pages,dry=True)
        self.assertNotEqual(result['pipelineState'],'VERIFIED_FACTS')
        with self.assertRaises(update.AutomationError):
            hbo.detect({'title':'Max renueva Hacks por una cuarta temporada','body':'','url':c['sourceUrl'],'sourceName':hbo.SOURCE},
                       {'tmdbId':c['tmdbId'],'title':'Hacks','aliases':['Hacks']},NOW.date())

    def test_wrong_show_and_provider_brand_mismatch(self):
        c=CORPUS['positives'][1]
        for heading in ('HBO renueva Another Show por una tercera temporada','Max renueva Rooster por una segunda temporada'):
            pages=self.pages(c);pages[c['sourceUrl']]=document(heading,'',c['sourceUrl'])
            result,_=self.process(c,pages=pages,dry=True);self.assertNotEqual(result['pipelineState'],'VERIFIED_FACTS')

    def test_identity_change_on_monitored_refetch_fails(self):
        c=CORPUS['positives'][1];calls=0;pages=self.pages(c)
        def fetch(url,domains):
            nonlocal calls
            if url==c['sourceUrl']:
                calls+=1
                return (pages[url] if calls==1 else document('Other Show','',url)),url
            return pages.get(url,'<h1>Index</h1>'),url
        report=discovery.discover(self.metadata(c),self.trusted,fetcher=fetch)
        result=monitored.process(c['tmdbId'],metadata=self.metadata(c),dry_run=True,discoverer=lambda *_:report,
            fetcher=fetch,today=NOW.date(),registry_path=self.stage,audit_path=self.audit)
        self.assertEqual(result['pipelineState'],'VALIDATION_FAILED')

    def test_redirect_and_wildcard_hosts_rejected(self):
        c=CORPUS['positives'][1]
        for final in ('https://other.max.com/post/x','https://press-spain.max.com.evil.test/post/x','http://press-spain.max.com/post/x'):
            pages=self.pages(c);pages[c['sourceUrl']]=(pages[c['sourceUrl']],final)
            result,_=self.process(c,pages=pages,dry=True);self.assertNotEqual(result['pipelineState'],'VERIFIED_FACTS')

    def test_hbo_transport_uses_qualified_decoder_and_no_wbd_bypass(self):
        with patch.object(hbo.qualified,'fetch',return_value=({'failureCategory':'HTTP_403'},None)) as fetch:
            with self.assertRaises(update.ProviderUnavailable):hbo.fetch('https://'+hbo.HOST+'/post/x',[hbo.HOST])
            self.assertEqual(fetch.call_count,1)
        with self.assertRaises(update.AutomationError):hbo.fetch('https://press.wbd.com/a',[hbo.HOST])

    def test_default_discovery_uses_hbo_transport(self):
        with patch.object(hbo,'fetch',side_effect=update.ProviderUnavailable('DNS_FAILURE')) as transport:
            result=discovery.discover(self.metadata(),self.trusted)
        self.assertEqual(result['result'],'provider_unavailable');self.assertTrue(transport.called)

    def test_publication_and_localized_identity_only_from_matching_tmdb(self):
        c=CORPUS['positives'][3];calls=[]
        def fetch(url,domains,headers):
            calls.append(url)
            value={'id':c['tmdbId'],'name':c['aliases'][-1] if 'es-ES' in url else c['requestedTitle'],'networks':[{'name':'HBO'}],'status':'Canceled'}
            return json.dumps(value),url
        meta=discovery.tmdb_metadata(c['tmdbId'],'fixture',fetcher=fetch)
        self.assertEqual(meta['hboSpanishTitle'],c['aliases'][-1]);self.assertEqual(len(calls),2);self.assertNotIn('status',meta)
        self.assertEqual(discovery.route_provider(meta,self.specs)[0],'HBO')

    def test_localized_metadata_wrong_id_not_an_alias(self):
        c=CORPUS['positives'][3]
        def fetch(url,domains,headers):
            return json.dumps({'id':999 if 'es-ES' in url else c['tmdbId'],'name':'Wrong' if 'es-ES' in url else c['requestedTitle'],'networks':[{'name':'HBO'}]}),url
        self.assertNotIn('hboSpanishTitle',discovery.tmdb_metadata(c['tmdbId'],'fixture',fetcher=fetch))

    def test_regional_es_date_never_global_and_cancellation_insufficient(self):
        c=copy.deepcopy(CORPUS['positives'][1]);c['identityResult']['articleHeading']='La serie original de HBO Rooster vuelve con su tercera y última temporada el 10 de abril de 2027'
        c['parserFacts']=[];result,_=self.process(c,dry=True)
        self.assertEqual(result['pipelineState'],'VERIFIED_FACTS',result)
        self.assertIsNone(result['verifiedFacts']['releaseDate']);self.assertIsNone(result['verifiedFacts']['releaseYear'])
        article=hbo.parse(document('La serie original de HBO Rooster ha sido cancelada','',c['sourceUrl']),c['sourceUrl'])
        self.assertEqual(hbo.detect(article,{'tmdbId':c['tmdbId'],'title':'Rooster','aliases':['Rooster']},NOW.date()),[])

    def test_real_negative_and_monthly_excerpts_remain_safe(self):
        self.assertEqual(len(CORPUS['negatives']),2);self.assertEqual(len(CORPUS['monthly']),1)
        for c in CORPUS['negatives']+CORPUS['monthly']:
            a={'title':c['identityResult'].get('articleHeading',''),'body':'\n'.join(c['relevantEvidenceSnippet']),
               'url':c['sourceUrl'],'sourceName':hbo.SOURCE,'language':'es','territory':'ES'}
            self.assertEqual(hbo.qualified.spanish.detect(a,{'tmdbId':c['tmdbId'],'title':c['requestedTitle'],'aliases':c['aliases']},NOW.date()),[])
        self.assertTrue(all(s['passed'] for s in hbo.qualified.shared_safety().values()))

    def test_seven_fields_tampering_blocks_promotion(self):
        c=CORPUS['positives'][1];self.process(c)
        original=json.loads(self.stage.read_bytes())
        for field in monitored.FACT_FIELDS:
            data=copy.deepcopy(original);row=data['series'][0];row['verifiedFacts'][field]= {'nextSeasonNumber':9,'releaseDate':'2027-01-01','releaseYear':2027,'status':'FINAL_SEASON','announcementDate':'2025-01-01','sourceName':'Other','sourceUrl':'https://'+hbo.HOST+'/post/other'}[field]
            self.stage.write_text(json.dumps(data),encoding='utf-8')
            self.assertFalse(self.promote(c['tmdbId'])['promotionEligible'],field)

    def test_persisted_network_or_missing_proof_blocks_promotion(self):
        c=CORPUS['positives'][1];self.process(c);original=json.loads(self.stage.read_bytes())
        for networks in (None,[{'name':'Max'}],[{'name':'HBO'},{'name':'Max'}]):
            data=copy.deepcopy(original)
            if networks is None:del data['series'][0]['providerIdentity']
            else:data['series'][0]['providerIdentity']['networks']=networks
            self.stage.write_text(json.dumps(data),encoding='utf-8');self.assertFalse(self.promote(c['tmdbId'])['promotionEligible'])

    def test_replay_mismatch_blocks_verified_facts_before_any_write(self):
        with patch.object(promotion,'_facts_summary',side_effect=promotion.EvidenceMismatch(['status'],{},{})):
            result,_=self.process()
        self.assertEqual(result['pipelineState'],'VALIDATION_FAILED');self.assertFalse(self.stage.exists())

    def test_dry_run_no_monitored_or_production_write(self):
        result,_=self.process(dry=True)
        self.assertEqual(result['pipelineState'],'VERIFIED_FACTS');self.assertFalse(self.stage.exists());self.assertFalse(self.audit.exists())

    def test_source_order_does_not_change_shared_merge_or_replay(self):
        c=CORPUS['positives'][1];url=c['sourceUrl'];second=url+'-alternate';pages=self.pages(c)
        pages[second]=pages[url].replace(url,second)
        results=[]
        for urls in ([url,second],[second,url]):
            pages[hbo.generic.ENTRY_POINTS[1]]='<h1>Index</h1>'+''.join(f'<a href="{u}">Rooster renewal</a>' for u in urls)
            result,_=self.process(c,pages=pages)
            self.assertEqual(result['pipelineState'],'VERIFIED_FACTS')
            self.assertTrue(self.promote(c['tmdbId'])['promotionEligible'])
            results.append(result['verifiedFacts'])
        self.assertEqual(*results)

    def test_production_negation_speculation_filming_teaser_not_lifecycle(self):
        c=CORPUS['positives'][1]
        for text in ('Rooster no ha sido renovada por una tercera temporada','Rooster podría ser renovada para una tercera temporada',
                     'Rooster comienza el rodaje de su tercera temporada','Rooster regresa con su tercera temporada',
                     'Rooster lanza el teaser de la tercera temporada','La tercera temporada de Rooster se estrena el 14 de abril'):
            pages=self.pages(c);pages[c['sourceUrl']]=document('La serie original de HBO Rooster',text,c['sourceUrl'])
            result,_=self.process(c,pages=pages,dry=True)
            self.assertEqual(result['pipelineState'],'NO_VERIFIED_FACTS',result)
            self.assertEqual(result['verifiedFacts'],{})

    def test_disabled_config_also_blocks_persisted_promotion(self):
        c=CORPUS['positives'][1];self.process(c)
        specs=copy.deepcopy(self.specs);specs['HBO']['productionActivated']=False
        with patch.object(discovery,'provider_specs',return_value=specs):
            self.assertFalse(self.promote(c['tmdbId'])['promotionEligible'])

    def test_shared_global_promotion_gate_and_workflow_unchanged(self):
        self.assertTrue(automatic_promotion.gate_enabled('true'))
        for value in ('false','TRUE','1','',None):self.assertFalse(automatic_promotion.gate_enabled(value))
        workflow=(update.ROOT/'.github/workflows/process-series-request.yml').read_text(encoding='utf-8')
        self.assertIn('vars.OFFICIAL_DATA_PROMOTION_ENABLED',workflow);self.assertIn('automatic_promotion.py',workflow)
        self.assertNotIn('hbo_production.py',workflow)

    def test_discovery_fetch_budget_exhaustion_is_fail_closed(self):
        with patch.object(hbo,'MAX_FETCHES',2):
            result,calls=self.process(dry=True)
        self.assertEqual(len(calls),2)
        self.assertNotEqual(result['pipelineState'],'VERIFIED_FACTS')


if __name__=='__main__':unittest.main()
