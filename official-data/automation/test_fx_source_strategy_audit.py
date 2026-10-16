"""Offline audit infrastructure contracts. No provider network or production writes."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

import fx_source_strategy_audit as fx

BASE = "https://www.fxnetworks.com"


def case(title="Series A", path="/shows/series-a/renewal", **extras):
    return {"caseId": "test", "series": title, "aliases": [title], "articleUrl": BASE + path, **extras}


def html(title, text=""):
    return f"<html><title>{title}</title><h1>{title}</h1><main><p>{text}</p></main></html>"


class FxSourceStrategyAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in fx.qualification.PROTECTED_FILES:
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'protected-fixture')

    def run_audit(self, cases, pages, roots=None):
        calls = []
        def fetch(url, domains):
            calls.append(url)
            if url not in pages:
                return {"status": "failed", "failureCategory": "FIXTURE_MISSING"}, None
            value = pages[url]
            if isinstance(value, tuple):
                return value
            return {"status": "fetched", "httpStatus": 200, "finalUrl": url}, value
        result = fx.run(root=self.root, cases=cases, roots=roots or [BASE + '/headlines'],
                        fetcher=fetch, today=dt.date(2026, 10, 12))
        self.assertLessEqual(len(calls), fx.MAX_FETCHES)
        return result

    def test_structures_separate_publication_from_discovery(self):
        for path, expected in [('/headlines','provider_index'),('/shows/all','provider_index'),
                               ('/shows/a','series_page'),('/shows/a/headlines','series_index'),
                               ('/shows/a/cast/person','biography'),('/news/story','announcement'),
                               ('/shows/a/renewal','show_subpage'),('/sitemap.xml','sitemap')]:
            self.assertEqual(fx.structure(BASE + path), expected)

    def test_show_page_never_counts_even_when_linked_from_provider_root(self):
        item = case(path='/shows/series-a')
        pages = {BASE + '/headlines': '<a href="/shows/series-a">Series A</a>',
                 item['articleUrl']: html('Series A', "Series A's sixth and final season begins now.")}
        report = self.run_audit([item], pages)
        row = report['discoveredLifecycleEvidence'][0]
        self.assertEqual(row['sourceStructure'], 'series_page')
        self.assertEqual(row['independentReconstruction']['result'], 'passed')
        self.assertEqual(report['providerRootAssessment']['qualifyingAnnouncementsFound'], 0)
        self.assertEqual(row['genericDiscovery'], 'GENERICALLY_DISCOVERABLE')

    def test_direct_probe_cannot_manufacture_generic_discovery(self):
        item = case()
        report = self.run_audit([item], {item['articleUrl']: html('Series A renewed for season 2')})
        row = report['discoveredLifecycleEvidence'][0]
        self.assertTrue(row['directProbeOnly'])
        self.assertIsNone(row['discoveryPath'])
        self.assertEqual(report['providerRootAssessment']['qualifyingAnnouncementsFound'], 0)

    def test_linked_announcement_replays_seven_fields(self):
        item = case()
        report = self.run_audit([item], {BASE + '/headlines': '<a href="/shows/series-a/renewal">Series A</a>',
            item['articleUrl']: html('Series A renewed for season 2')})
        row = report['discoveredLifecycleEvidence'][0]
        self.assertEqual(row['sourceStructure'], 'announcement')
        self.assertEqual(report['providerRootAssessment']['qualifyingAnnouncementsFound'], 1)
        self.assertEqual(row['independentReconstruction']['mismatchedFields'], [])
        self.assertEqual(len(row['independentReconstruction']['fieldsCompared']), 7)
        self.assertFalse(report['productionActivationRecommended'])

    def test_generic_title_and_wrong_redirect_rejected_without_relaxing_identity(self):
        item = case('Far Cry', '/news/far-cry', kind='negative')
        samples = [html('Fearless', '<h2>New cast for Far Cry</h2>'),
                   ({'status':'fetched','httpStatus':200,'finalUrl':BASE+'/shows/other'}, html('Far Cry')),
                   ({'status':'fetched','httpStatus':200,'finalUrl':'https://fxnetworks.com.evil.example/'}, html('Far Cry'))]
        for value in samples:
            with self.subTest(value=value):
                row = self.run_audit([item], {item['articleUrl']: value})['discoveredLifecycleEvidence'][0]
                self.assertFalse(row['identity']['acceptedForAudit'])
                self.assertIsNone(row['verifiedProductionFact'])
                self.assertFalse((row['negativeSafety'] or {}).get('qualificationCoveragePassed',False))

    def test_negative_content_pass_and_identity_rejection_are_distinct(self):
        content = {'facts': []}
        rejected = fx.negative_diagnostic(content, False)
        self.assertTrue(rejected['safeRejectionObserved'])
        self.assertFalse(rejected['targetNegativeContentValidated'])
        accepted = fx.negative_diagnostic(content, True)
        self.assertTrue(accepted['qualificationCoveragePassed'])
        self.assertIsNone(accepted['verifiedFacts'])
        self.assertFalse(fx.negative_diagnostic({'facts':[{'status':'RENEWED'}]},False)['safeRejectionObserved'])

    def test_cross_title_and_unbound_mayans_create_no_production_fact(self):
        for title, text in [('Mayans MC','<h2>FINAL SEASON</h2><h2>Season 5</h2>'),
                            ('Series B', 'Series A returns for its fifth season. Series B has its final season.'),
                            ('Series A','Series A has not been renewed for season 2.'),
                            ('Series A','Series A may return for season 2.'),
                            ('Series A','Cast announced for season 2 of Series A.')]:
            item = case(title, '/shows/test')
            row = self.run_audit([item], {item['articleUrl']: html(title,text)})['discoveredLifecycleEvidence'][0]
            self.assertEqual(row['parserFactsDiagnosticOnly'], [])
            self.assertIsNone(row['verifiedProductionFact'])

    def test_navigation_and_recommendation_cards_are_not_evidence(self):
        item = case()
        raw = html('Series A', 'Cast news only.') + '<nav>Series A renewed for season 5.</nav><aside>Series A final season 5</aside>'
        row = self.run_audit([item], {item['articleUrl']:raw})['discoveredLifecycleEvidence'][0]
        self.assertEqual(row['parserFactsDiagnosticOnly'], [])

    def test_artifact_schema_and_integrity(self):
        report = self.run_audit([], {})
        for field in ('schemaVersion','generatedAt','provider','auditOnly','readOnly','currentQualification',
                      'discoveredLifecycleEvidence','sourceStructureAnalysis','providerRootAssessment',
                      'mayansDiagnostic','farCryNegativeSafetyDiagnostic','genericDiscoveryAssessment',
                      'recommendedQualificationStrategy','productionActivationRecommended','protectedFileIntegrity'):
            self.assertIn(field, report)
        json.dumps(report)
        integrity = report['protectedFileIntegrity']
        self.assertEqual(integrity['before'],integrity['after'])
        self.assertEqual(integrity['changedFiles'],[])
        self.assertTrue(report['auditOnly'] and report['readOnly'])

    def test_protected_mutation_aborts(self):
        def fetch(url, domains):
            (self.root / fx.qualification.PROTECTED_FILES[0]).write_bytes(b'changed')
            return {'status':'failed'},None
        with self.assertRaisesRegex(RuntimeError,'Protected files changed'):
            fx.run(root=self.root, cases=[], roots=[BASE], fetcher=fetch)

    def test_manual_workflow_has_no_writer_credentials_or_backend(self):
        root = Path(__file__).resolve().parents[2]
        workflow = (root/'.github/workflows/audit-fx-source-strategy.yml').read_text(encoding='utf-8')
        for required in ('name: FX Source Strategy Audit','workflow_dispatch:', 'contents: read',
                         'persist-credentials: false','sha256sum -c','git diff --exit-code HEAD',
                         'name: fx-source-strategy-audit', 'fx_source_strategy_audit.py'):
            self.assertIn(required,workflow)
        for prohibited in ('schedule:', 'contents: write','git push','git commit','secrets.',
                           'promotion.py','update.py','pythonanywhere','assembleDebug','package_release.py'):
            self.assertNotIn(prohibited,workflow)

    def test_artifact_writer_rejects_credential_material(self):
        with self.assertRaisesRegex(ValueError, 'Credential scan rejected'):
            fx.checked_payload({'providerText': 'ghp_' + 'X' * 30})


if __name__ == '__main__':
    unittest.main()
