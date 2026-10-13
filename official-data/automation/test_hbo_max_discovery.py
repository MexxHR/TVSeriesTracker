"""Synthetic discovery contracts; never count these as real provider coverage."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import hbo_max_discovery as d
import hbo_max_qualification as q
import test_hbo_max_qualification as fixtures
from test_hbo_max_qualification import case, html, article, ENTRY, TODAY, URL


class DiscoveryTests(unittest.TestCase):
    def crawl(self, pages, titles=('SERIES A',)):
        self.calls = []
        def get(url):
            self.calls.append(url)
            return {'status':'fetched','finalUrl':url},pages.get(url)
        return d.crawl(get,titles)

    def test_html_sitemap_nav_links_are_discovery_not_evidence(self):
        rows,diag=self.crawl({d.ENTRY_POINTS[1]:'<nav><ul><li><a href="'+URL+'">SERIES <b>A</b></a></li></ul></nav>'})
        self.assertEqual(rows[0]['url'],URL)
        self.assertEqual(rows[0]['path'],[d.ENTRY_POINTS[1],URL])
        self.assertFalse(q.evidence_url(d.ENTRY_POINTS[1]))
        self.assertEqual(diag['sitemap']['pages'][0]['format'],'HTML')

    def test_xml_urlset_lastmod_only_discovery(self):
        rows,shape=d.extract('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>'+URL+'</loc><lastmod>2026-01-01</lastmod></url></urlset>')
        self.assertEqual(rows,[(URL,'')]);self.assertTrue(shape['lastmodPresent'])

    def test_xml_child_sitemap_is_followed(self):
        child='https://'+d.HOST+'/sitemap-archive.xml'
        rows,_=self.crawl({d.ENTRY_POINTS[1]:'<sitemapindex><sitemap><loc>'+child+'</loc></sitemap></sitemapindex>',child:'<urlset><url><loc>'+URL+'</loc></url></urlset>'})
        self.assertEqual(rows[0]['path'],[d.ENTRY_POINTS[1],child,URL])

    def test_xml_entities_rejected(self):
        rows,shape=d.extract('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY x "bad">]><urlset><loc>&x;</loc></urlset>')
        self.assertEqual(rows,[]);self.assertEqual(shape['format'],'XML_REJECTED')

    def test_post_listing_and_observed_pagination(self):
        page=d.canonical(d.ENTRY_POINTS[2]+'?limit=30&offset=0&searchKey=&tab=all')
        rows,diag=self.crawl({d.ENTRY_POINTS[2]:'<nav><a href="'+page+'">Ver más</a></nav>',page:'<article><a href="'+URL+'">SERIES A</a></article>'},titles=())
        self.assertEqual(rows[0]['path'],[d.ENTRY_POINTS[2],page,URL])
        self.assertTrue(diag['postIndex']['paginationFollowed'])

    def test_public_search_requires_observed_template(self):
        template=d.canonical(d.ENTRY_POINTS[2]+'?limit=30&offset=0&searchKey=&tab=all')
        search=d.canonical(d.ENTRY_POINTS[2]+'?limit=30&offset=0&searchKey=SERIES+A&tab=all')
        rows,diag=self.crawl({d.ENTRY_POINTS[2]:'<a href="'+template+'">Ver más</a>',search:'<a href="'+URL+'">SERIES A</a>'})
        self.assertEqual(rows[0]['url'],URL)
        self.assertEqual(diag['search']['status'],'OBSERVED_GET_TEMPLATE')
        self.assertIn(search,self.calls)
        self.crawl({});self.assertEqual(self.calls,list(d.ENTRY_POINTS))

    def test_canonical_article_dedup_and_fragment_query(self):
        rows,_=self.crawl({d.ENTRY_POINTS[1]:'<a href="'+URL+'/?utm_source=x#x">SERIES A</a><a href="'+URL+'">SERIES A</a>'})
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['url'],URL)
        self.assertEqual(d.canonical('https://PRESS-SPAIN.MAX.COM:443/post/x/?a=1#y'),'https://'+d.HOST+'/post/x')

    def test_query_preserved_for_pagination_but_invalid_rejected(self):
        self.assertNotEqual(d.canonical(d.ENTRY_POINTS[2]+'?offset=0'),d.canonical(d.ENTRY_POINTS[2]+'?offset=30'))
        for query in ('offset=-1','offset=10001','limit=999','token=secret','tab=../x','limit=30&limit=45'):
            self.assertIsNone(d.canonical(d.ENTRY_POINTS[2]+'?'+query))

    def test_exact_host_https_no_credentials_ports_or_encoded_traversal(self):
        for url in ('http://'+d.HOST+'/post/x','https://evil.test/post/x','https://'+d.HOST+'.evil.test/post/x',
                    'https://other.max.com/post/x','https://u:p@'+d.HOST+'/post/x','https://'+d.HOST+':444/post/x',
                    'https://'+d.HOST+'/post/%2e%2e/private'):
            self.assertIsNone(d.canonical(url),url)

    def test_external_script_and_unobserved_urls_not_followed(self):
        rows,_=self.crawl({d.ENTRY_POINTS[1]:'<a href="https://evil.test/post/x">SERIES A</a><script><a href="'+URL+'">SERIES A</a></script>'})
        self.assertEqual(rows,[]);self.assertEqual(self.calls,list(d.ENTRY_POINTS))

    def test_redirected_index_to_article_not_discovery_proof(self):
        rows,diag=d.crawl(lambda u:({'finalUrl':URL},'<a href="'+URL+'">SERIES A</a>'),[])
        self.assertEqual(rows,[]);self.assertTrue(all(not p['fetched'] for p in diag['pages']))

    def test_index_budget_and_cycles(self):
        links=''.join(f'<a href="/post?offset={i}">Next</a>' for i in range(100))
        calls=[]
        def get(u):calls.append(u);return {'finalUrl':u},links
        _,diag=d.crawl(get,[])
        self.assertEqual(len(calls),d.MAX_INDEX_PAGES);self.assertTrue(diag['truncated'])

    def test_candidate_budget_deterministic_lifecycle_ranking(self):
        rows=[{'url':'https://'+d.HOST+'/post/series-a-'+str(i),'anchor':'SERIES A teaser','path':[]} for i in range(20)]
        rows.append({'url':URL,'anchor':'HBO renueva SERIES A por una tercera temporada','path':[]})
        selected,count=d.candidates(rows,['SERIES A'],'HBO')
        self.assertEqual(count,21);self.assertEqual(len(selected),d.MAX_CANDIDATES);self.assertEqual(selected[0]['url'],URL)
        self.assertEqual(selected,d.candidates(list(reversed(rows)),['SERIES A'],'HBO')[0])

    def test_title_case_punctuation_apostrophes_and_html_entities(self):
        for text in ('¡Renovación: CONAN O’BRIEN!',"Conan O&#39;Brien",'conan o\'brien'):
            self.assertTrue(d.title_matches(text,["Conan O'Brien"]))
        self.assertTrue(d.title_matches('¡ÁREA: novedades!',['ÁREA']))
        self.assertTrue(d.title_matches('IT: Bienvenidos a Derry',['IT: Bienvenidos a Derry']))
        rows=[{'url':'https://'+d.HOST+'/post/max-renueva-conan-obrien','anchor':'','path':[]}]
        self.assertEqual(d.candidates(rows,["Conan O'Brien"],'MAX')[1],1)

    def test_similar_titles_accents_and_partial_tokens_rejected(self):
        for text,alias in [('SERIES ABC','SERIES A'),('AREA','ÁREA'),('Tokyovice','Tokyo Vice'),('Hacksmith','Hacks')]:
            self.assertFalse(d.title_matches(text,[alias]))


class QualificationDiscoveryTests(unittest.TestCase):
    setUp=fixtures.QualificationTests.setUp
    run_q=fixtures.QualificationTests.run_q

    def test_positive_oracle_is_never_fetched_without_index(self):
        calls=[]
        def fetch(url,domains):calls.append(url);return {'status':'fetched','finalUrl':url},html('HBO renueva SERIES A por una tercera temporada') if url==URL else None
        result=self.run_q([case()],{},fetcher=fetch)
        self.assertNotIn(URL,calls)
        row=result['hbo']['cases'][0]
        self.assertFalse(row['passed']);self.assertFalse(row['knownUrlUsedAsSeed'])
        self.assertEqual(result['hbo']['distinctPositiveSeries'],0)

    def test_discovery_success_without_any_oracle(self):
        c=case();c['knownUrls']=[]
        result=self.run_q([c],{q.ROOTS[1]:'<nav><a href="'+URL+'">HBO renueva SERIES A por una tercera temporada</a></nav>',URL:html('HBO renueva SERIES A por una tercera temporada')})
        row=result['hbo']['cases'][0]
        self.assertTrue(row['passed']);self.assertEqual(row['discoveryEntryPoint'],q.ROOTS[1])
        self.assertEqual(row['mismatchedFields'],[]);self.assertEqual(len(row['independentReconstruction']['fieldsCompared']),7)
        self.assertEqual(result['hbo']['distinctPositiveSeries'],1)

    def test_changed_oracle_cannot_change_positive_discovery_or_selection(self):
        pages={q.ROOTS[1]:'<a href="'+URL+'">SERIES A</a>',URL:html('HBO renueva SERIES A por una tercera temporada')}
        a=self.run_q([case()],pages)['hbo']['cases'][0]
        c=case();c['knownUrls']=['https://'+q.HOST+'/post/other-show']
        b=self.run_q([c],pages)['hbo']['cases'][0]
        self.assertEqual(a['attempts'],b['attempts']);self.assertEqual(a['parserFacts'],b['parserFacts'])
        self.assertTrue(a['oracleMatched']);self.assertFalse(b['oracleMatched'])

    def test_routing_ambiguity_does_not_count_discovered_fact(self):
        pages={q.ROOTS[1]:'<a href="'+URL+'">SERIES A</a>',URL:html('HBO renueva SERIES A por una tercera temporada')}
        result=self.run_q([case()],pages,metadata={'title':'SERIES A','networks':[{'name':'HBO'},{'name':'Max'}]})
        self.assertFalse(result['hbo']['cases'][0]['passed']);self.assertEqual(result['hbo']['distinctPositiveSeries'],0)
        self.assertEqual(result['hbo']['cases'][0]['failureReason'],'AMBIGUOUS_NETWORKS')

    def test_discovered_article_replay_mismatch_never_counts(self):
        pages={q.ROOTS[1]:'<a href="'+URL+'">SERIES A</a>',URL:html('HBO renueva SERIES A por una tercera temporada')}
        with patch.object(q,'reconstruction',return_value={'result':'mismatch','mismatchedFields':['status']}):
            result=self.run_q([case()],pages)
        self.assertFalse(result['hbo']['cases'][0]['passed']);self.assertEqual(result['hbo']['distinctPositiveSeries'],0)

    def test_failed_or_direct_rows_do_not_inflate_any_counter(self):
        row={'tmdbId':1,'kind':'positive','contentPassed':True,'passed':False,'discoveryMethod':'DIRECT_KNOWN_URL_ONLY','independentReconstruction':{'result':'passed'}}
        result=q.qualification_state([row],{'safety':{'passed':True}},True,True)
        self.assertEqual([result[k] for k in ('distinctPositiveSeries','genericDiscoveryPassingSeries','independentReconstructionPassingSeries')],[0,0,0])

    def test_two_series_remain_below_unchanged_threshold(self):
        row={'kind':'positive','contentPassed':True,'passed':True,'discoveryMethod':'DISCOVERED_FROM_FIRST_PARTY_INDEX','independentReconstruction':{'result':'passed'}}
        result=q.qualification_state([{**row,'tmdbId':i} for i in (1,2)],{'safety':{'passed':True}},True,True)
        self.assertEqual(result['qualificationState'],'INSUFFICIENT_DISTINCT_FIRST_PARTY_CASES');self.assertEqual(result['minimumDistinctSeries'],3)

    def test_reconstruction_failure_still_blocks_even_with_three_other_passes(self):
        row={'kind':'positive','contentPassed':True,'passed':True,'discoveryMethod':'DISCOVERED_FROM_FIRST_PARTY_INDEX','independentReconstruction':{'result':'passed'}}
        rows=[{**row,'tmdbId':i} for i in (1,2,3)]
        rows.append({**row,'tmdbId':4,'passed':False,'independentReconstruction':{'result':'mismatch'},'mismatchedFields':['status']})
        result=q.qualification_state(rows,{'safety':{'passed':True}},True,True)
        self.assertEqual(result['distinctPositiveSeries'],3)
        self.assertEqual(result['qualificationState'],'PARSER_COMPATIBILITY_NEEDS_WORK')

    def test_real_source_una_final_phrase_generic_and_adversarial(self):
        for title in ('SERIES A','OTHER SHOW'):
            entry={**ENTRY,'title':title,'aliases':[title]}
            facts=q.spanish.detect(article(title+' vuelve con una tercera y última temporada'),entry,TODAY)
            self.assertEqual((facts[0]['status'],facts[0]['nextSeasonNumber']),('FINAL_SEASON',3))
            merged,_=q.update.merge(None,facts,TODAY)
            expected={k:merged.get(k) for k in q.promotion.monitored.FACT_FIELDS}
            replay=q.reconstruction(facts,[],html(title+' vuelve con una tercera y última temporada'),URL,entry,TODAY,expected)
            self.assertEqual(replay['result'],'passed');self.assertEqual(replay['mismatchedFields'],[])
        for text in ('SERIES A no vuelve con una tercera y última temporada','SERIES A podría volver con una tercera y última temporada',
                     'SERIES B vuelve con una tercera y última temporada','SERIES A vuelve con una última temporada',
                     'SERIES A vuelve con una tercera temporada. SERIES B vive su última temporada'):
            self.assertEqual(q.spanish.detect(article(text),ENTRY,TODAY),[])

    def test_regional_date_and_negative_safety_unchanged(self):
        facts=q.spanish.detect(article('SERIES A vuelve con una tercera y última temporada'),ENTRY,TODAY)
        self.assertIsNone(facts[0]['releaseDate']);self.assertIsNone(facts[0]['releaseYear'])
        self.assertTrue(all(v['passed'] for v in q.shared_safety().values()))

    def test_schema_discovery_diagnostics_and_baseline_hash(self):
        result=self.run_q([case()],{})
        self.assertEqual(result['baseline']['artifactSha256'],q.BASELINE_SHA)
        self.assertFalse(result['discovery']['knownUrlSeedingUsed'])
        for f in ('discoveryEntryPoint','candidateCountBeforeFilter','candidateCountAfterFilter','discoveredUrl','knownUrlOracle','oracleMatched','knownUrlUsedAsSeed','sourceName','sourceUrl','announcementDate','providerIdentityMatched'):
            self.assertIn(f,result['hbo']['cases'][0])
        self.assertTrue(result['protectedFileIntegrity']['unchanged'])

    def test_shared_first_party_http_budget_is_hard_limit(self):
        calls=[]
        def fetch(url,domains):
            calls.append(url)
            return {'status':'fetched','finalUrl':url},'<a href="/post?offset=30">Next</a>'
        with patch.object(q,'MAX_FETCHES',2):
            result=self.run_q([],{},fetcher=fetch)
        self.assertEqual(len(calls),2)
        self.assertEqual(result['budget']['actualFetches'],2)
        self.assertEqual(result['budget']['maximumFetches'],2)
        self.assertEqual(result['productionActivation'],{'HBO':False,'MAX':False})

    def test_regional_diagnostic_does_not_become_positive_lifecycle(self):
        pages={q.ROOTS[1]:'<a href="'+URL+'">SERIES A</a>',URL:html('La tercera temporada de la serie original de HBO SERIES A se estrena el 14 de abril')}
        result=self.run_q([case()],pages)
        row=result['hbo']['cases'][0]
        self.assertFalse(row['passed']);self.assertEqual(row['parserFacts'],[])
        self.assertIsNone(row['releaseDate']);self.assertIsNone(row['releaseYear'])

    def test_real_negative_corpus_documents_preserved(self):
        config=json.loads(q.CONFIG.read_text(encoding='utf-8'))
        negatives=[c for c in config['cases'] if c['kind']=='negative']
        self.assertEqual({c['caseId'] for c in negatives},{'white-lotus-spanish-filming','pitt-spanish-teaser'})
        self.assertTrue(all(c['knownUrlOracle'] for c in negatives))
        self.assertTrue(any(c['kind']=='ambiguity' for c in config['cases']))


if __name__=='__main__':
    unittest.main()
