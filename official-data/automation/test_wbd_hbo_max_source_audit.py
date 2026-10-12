"""Offline audit contracts; synthetic parser fixtures are never provider evidence."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wbd_hbo_max_source_audit as wbd

BASE = 'https://press.wbd.com'
DOMAINS = ['press.wbd.com','hbo.com','max.com','press-spain.max.com']

def item(title='Series A', **extras):
    return dict(caseId='fixture',series=title,aliases=[title],tmdbId=1,
                articleUrl=BASE+'/us/media-release/series-a',kind='positive',goal='synthetic fixture',**extras)

def html(title, text=''):
    return f'<title>{title}</title><h1>{title}</h1><main><p>{text}</p></main>'

class WbdAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in wbd.replay.PROTECTED_FILES:
            p = self.root/name
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text('{"series":[]}' if name.endswith('sources.json') else 'protected',encoding='utf-8')

    def run_audit(self, cases=None, pages=None, roots=None, fetcher=None):
        config = {'cases':cases or [],'domains':[{'host':h,'evidenceEligible':True} for h in DOMAINS],
                  'roots':roots or [BASE+'/index']}
        pages = pages or {}
        def fetch(url, domains):
            value = pages.get(url)
            if isinstance(value,tuple):
                return value
            return ({'status':'fetched','httpStatus':200,'finalUrl':url},value) if value else ({'status':'failed','failureCategory':'DNS_FAILURE'},None)
        return wbd.run(root=self.root,config=config,fetcher=fetcher or fetch,today=dt.date(2026,10,12))

    def row(self, text, title='Series A', **extras):
        c=item(title,**extras)
        return self.run_audit([c],{c['articleUrl']:html(title,text)})['auditCases'][0]

    def test_hbo_network_only(self):
        self.assertEqual(wbd.proposed_route({'networks':[{'name':'HBO'}]})['route'],'HBO')

    def test_max_network_aliases(self):
        for label in ('Max','HBO Max'):
            self.assertEqual(wbd.proposed_route({'networks':[{'name':label}]})['route'],'MAX')

    def test_ownership_homepage_and_other_wbd_networks_not_routes(self):
        for data in ({'productionCompanies':['HBO'],'homepage':'https://hbo.com'},
                     {'networks':[{'name':'Warner Bros. Discovery'}]}, {'networks':[{'name':'TNT'}]},
                     {'networks':['HBO','Max']},{'networks':['HBO','BBC One']}):
            self.assertIsNone(wbd.proposed_route(data)['route'])

    def test_tmdb_metadata_discards_lifecycle(self):
        c=item()
        result=wbd.metadata_observations([c],'fixture',lambda i,t:{'title':'Series A','networks':[{'id':49,'name':'HBO'}],'status':'Canceled','seasons':[4]})
        self.assertNotIn('Canceled',json.dumps(result))
        self.assertNotIn('seasons',json.dumps(result))
        self.assertIsNone(wbd.metadata_observations([c],'')[1]['networks'])

    def test_ecosystem_source_families(self):
        for url,expected in [(BASE+'/us/media-release/a','WBD_PRESS_ARTICLE'),(BASE+'/us/brands/a','WBD_NEWSROOM'),
                             ('https://hbo.com/a','HBO_SHOW_PAGE'),('https://max.com/a','MAX_SHOW_PAGE'),
                             ('https://press-spain.max.com/post/a','MAX_PRESS')]:
            self.assertEqual(wbd.family(url),expected)

    def test_strict_domains_and_https(self):
        for url in ('http://hbo.com/a','https://hbo.com.evil.test/a','https://evil.hbo.com/a',
                    'https://hbo.com@evil.test/a','https://127.0.0.1/a','https://skydance.com/a'):
            self.assertFalse(wbd.allowed(url,DOMAINS),url)
        self.assertTrue(wbd.allowed('https://www.hbo.com/a',DOMAINS))

    def test_redirect_instrumentation_restored_and_guarded(self):
        original=wbd.audit.BoundedRedirect
        class Req:
            full_url=BASE+'/a'
        def fetch(url,domains):
            guard=wbd.audit.BoundedRedirect(domains)
            with self.assertRaises(wbd.audit.RedirectRejected):
                guard.redirect_request(Req(),None,302,'',{},'https://evil.test/a')
            return {'status':'failed','httpStatus':403},None
        with patch.object(wbd.audit,'fetch_url',fetch):
            result,raw=wbd.traced_fetch(BASE+'/a',DOMAINS)
        self.assertIs(wbd.audit.BoundedRedirect,original)
        self.assertFalse(result['redirectChain'][0]['withinAuditBoundary'])
        self.assertIsNone(raw)

    def test_wrong_show_http200_and_canonical_mismatch(self):
        c=item()
        for raw,final in [(html('Other Show','Series A renewed for season 3.'),c['articleUrl']),
                          (html('Series A'),BASE+'/generic'),
                          (html('Series A')+'<link rel="canonical" href="/other">',c['articleUrl'])]:
            self.assertFalse(wbd.identity(raw,c['articleUrl'],final,c,DOMAINS)['acceptedForAudit'])

    def test_access_classification(self):
        for status,expected in ((403,'HTTP_403'),(404,'HTTP_404'),(429,'RATE_LIMITED')):
            self.assertEqual(wbd.access_class({'httpStatus':status},None),expected)
        self.assertEqual(wbd.access_class({'failureCategory':'DNS_FAILURE'},None),'DNS_FAILURE')
        self.assertEqual(wbd.access_class({'status':'fetched'},'captcha'),'BOT_BLOCKED')
        self.assertEqual(wbd.access_class({'status':'fetched','redirectCount':1},'body'),'REDIRECTED_ACCESSIBLE')
        self.assertEqual(wbd.access_class({},None),'OTHER_NETWORK_FAILURE')

    def test_multititle_does_not_borrow_renewal(self):
        self.assertEqual(self.row('Series B renewed for season 3. Series A returns next year.')['parserFactsDiagnosticOnly'],[])

    def test_cross_season_does_not_borrow_final_number(self):
        facts=self.row('Series A has its final season. Series B premieres its fourth season in 2027.')['parserFactsDiagnosticOnly']
        self.assertFalse(any(f.get('season')==4 for f in facts))

    def test_navigation_and_sidebar_contamination(self):
        c=item()
        raw=html('Series A','Cast news only.')+'<nav>Series A renewed for season 5.</nav><aside>Series A final season 5.</aside>'
        self.assertEqual(self.run_audit([c],{c['articleUrl']:raw})['auditCases'][0]['parserFactsDiagnosticOnly'],[])

    def test_two_negative_forms_and_unavailable_never_pass(self):
        for text in ('Cast announced for season 2 of Series A.','Series A begins filming season 2.'):
            c=item(); c['kind']='negative'
            row=self.run_audit([c],{c['articleUrl']:html('Series A',text)})['auditCases'][0]
            self.assertEqual(row['negativeSafety']['lifecycleFacts'],[])
            self.assertTrue(row['negativeSafety']['targetContentValidated'])
        self.assertFalse(self.run_audit([c])['auditCases'][0]['negativeSafety']['targetContentValidated'])

    def test_language_gap_is_not_negative_pass(self):
        c=item(language='es'); c['kind']='negative'
        row=self.run_audit([c],{c['articleUrl']:html('Series A','Comienza el rodaje.')})['auditCases'][0]
        self.assertEqual(row['parserGap'],'PARSER_GAP_LANGUAGE_NOT_SUPPORTED')
        self.assertFalse(row['negativeSafety']['targetContentValidated'])

    def test_date_precision_and_window(self):
        for text in ('Series A season 2 premieres in 2027.','Series A season 2 premieres in Spring 2027.'):
            self.assertFalse(any(f.get('releaseDate') for f in self.row(text)['parserFactsDiagnosticOnly']))

    def test_explicit_date_and_year_remain_distinct(self):
        exact=self.row('Series A season 2 premieres January 15, 2027.')['parserFactsDiagnosticOnly']
        year=self.row('Series A season 2 premieres in 2027.')['parserFactsDiagnosticOnly']
        self.assertTrue(any(f.get('releaseDate')=='2027-01-15' for f in exact),exact)
        self.assertTrue(any(f.get('releaseYear')==2027 and not f.get('releaseDate') for f in year),year)

    def test_renewal_season_does_not_borrow_previous_season_date(self):
        row=self.row('Series A renewed for season 3. Series A season 2 premieres January 15, 2027.')
        facts=row['parserFactsDiagnosticOnly']
        self.assertTrue(any(f.get('status')=='RENEWED' and f.get('season')==3 for f in facts),facts)
        self.assertFalse(any(f.get('season')==3 and f.get('releaseDate') for f in facts),facts)

    def test_actual_links_only_discovery_path(self):
        c=item()
        row=self.run_audit([c],{BASE+'/index':'<a href="/us/media-release/series-a">Series A</a>',
                              c['articleUrl']:html('Series A','Series A renewed for season 2.')})['auditCases'][0]
        self.assertFalse(row['directProbeOnly'])
        self.assertEqual(row['discoveryPath'],[BASE+'/index',c['articleUrl']])
        self.assertEqual(row['genericDiscovery'],'DISCOVERABLE_WITH_LIMITATIONS')

    def test_brand_original_is_separate_from_platform_distribution(self):
        self.assertEqual(wbd.brand_identity(html('Series A','HBO Original Series A will stream on Max.'),'Series A'),['HBO'])
        self.assertEqual(wbd.brand_identity(html('Series A','Max Original Series A from Warner Bros. Television.'),'Series A'),['MAX'])

    def test_challenge_body_cannot_be_replayed(self):
        row=self.row('Verify you are human. Series A renewed for season 2.')
        self.assertEqual(row['parserFactsDiagnosticOnly'],[])
        self.assertEqual(row['failureStage'],'fetch')

    def test_public_url_removes_userinfo_query_fragment(self):
        self.assertEqual(wbd.public_url('https://name:private@hbo.com/a?value=private#x'),'https://hbo.com/a')

    def test_cancellation_non_inference(self):
        for text in ('Series A has no plans for another season.','Series A is missing from this slate.'):
            self.assertFalse(any(f.get('status')=='CANCELED' for f in self.row(text)['parserFactsDiagnosticOnly']))
        self.assertEqual(self.run_audit()['cancellationCoverage']['status'],'CANCELLATION_FIRST_PARTY_COVERAGE_INSUFFICIENT')

    def test_discovery_is_not_evidence_and_direct_probe_not_discovery(self):
        c=item(); c['articleUrl']=BASE+'/index'
        row=self.run_audit([c],{c['articleUrl']:html('Series A renewed for season 2')})['auditCases'][0]
        self.assertEqual(row['independentReconstruction']['result'],'not_applicable')
        self.assertFalse(row['discoveryIsEvidence'])
        self.assertTrue(row['directProbeOnly'])

    def test_independent_replay_seven_fields(self):
        row=self.row('Series A renewed for season 2.')
        result=row['independentReconstruction']
        self.assertEqual(result['result'],'passed',result)
        self.assertEqual(len(result['fieldsCompared']),7)
        self.assertEqual(result['mismatchedFields'],[])
        self.assertIsNone(row['verifiedProductionFact'])

    def test_independent_refetch_wrong_identity_rejected(self):
        c=item(); calls=0
        def fetch(url,domains):
            nonlocal calls
            if url!=c['articleUrl']:
                return {'status':'failed'},None
            calls+=1
            return {'status':'fetched','httpStatus':200,'finalUrl':url},html('Series A' if calls==1 else 'Other Show','Series A renewed for season 2.')
        row=self.run_audit([c],fetcher=fetch)['auditCases'][0]
        self.assertEqual(row['independentReconstruction']['result'],'failed')

    def test_independent_replay_reports_mismatched_field(self):
        original=wbd.replay.promotion._facts_summary
        def mismatch(*args,**kwargs):
            return {**original(*args,**kwargs),'sourceName':'changed fixture provenance'}
        with patch.object(wbd.replay.promotion,'_facts_summary',mismatch):
            result=self.row('Series A renewed for season 2.')['independentReconstruction']
        self.assertEqual(result['result'],'mismatch')
        self.assertIn('sourceName',result['mismatchedFields'])

    def test_artifact_schema_and_integrity(self):
        result=self.run_audit()
        for key in ('routingAssessment','domains','httpAccessMatrix','auditCases','whiteLotusDiagnostic',
                    'sourceFamilyAnalysis','discoveryAssessment','identityAssessment','parserAssessment',
                    'negativeSafetyAssessment','cancellationCoverage','releaseDateCoverage',
                    'independentReconstructionAssessment','recommendedProviderModel','qualificationStrategyRecommendation'):
            self.assertIn(key,result)
        self.assertEqual(result['schemaVersion'],1)
        self.assertTrue(result['readOnly'] and result['auditOnly'])
        integrity=result['protectedFileIntegrity']
        self.assertEqual(integrity['before'],integrity['after'])
        self.assertEqual(integrity['changedFiles'],[])
        self.assertFalse(result['productionActivationRecommended'])
        json.dumps(result)

    def test_protected_mutation_aborts(self):
        def fetch(url,domains):
            (self.root/wbd.replay.PROTECTED_FILES[0]).write_text('changed')
            return {'status':'failed'},None
        with self.assertRaisesRegex(RuntimeError,'Protected files changed'):
            self.run_audit(fetcher=fetch)

    def test_manual_workflow_read_only(self):
        root=Path(__file__).resolve().parents[2]
        workflow=(root/'.github/workflows/audit-wbd-hbo-max.yml').read_text()
        for required in ('name: WBD HBO Max Source Audit','workflow_dispatch:','contents: read',
                         'persist-credentials: false','sha256sum -c','git diff --exit-code HEAD',
                         'wbd_hbo_max_source_audit.py','name: wbd-hbo-max-source-audit'):
            self.assertIn(required,workflow)
        for forbidden in ('schedule:','push:','contents: write','git push','git commit','promotion.py',
                          'monitored.py','update.py','package_release.py','assembleDebug','pythonanywhere'):
            self.assertNotIn(forbidden,workflow)
        self.assertEqual(workflow.count('secrets.'),1)
        self.assertIn('secrets.TMDB_API_TOKEN',workflow)

    def test_credential_artifact_rejected(self):
        with self.assertRaisesRegex(ValueError,'Credential scan rejected'):
            wbd.checked_payload({'text':'ghp_'+'X'*30})

if __name__=='__main__':
    unittest.main()
