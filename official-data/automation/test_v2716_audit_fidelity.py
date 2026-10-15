"""V2.7.16 audit integration: frozen production paths, deterministic local model.

Identity response shapes have explicit provenance; announcement/index HTML is
synthetic forensic replay, never fresh network evidence or provider coverage.
"""
import copy
import datetime as dt
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import discovery
import provider_coverage_audit as audit
import provider_coverage_fidelity as fidelity
import v2716_test_support as historical

CAPTURE = historical.CAPTURE
OPTIONS = historical.OPTIONS
TODAY = historical.TODAY
replay_case = historical.replay_case


class AuditFidelityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.before = audit.protected_hashes(), audit.frozen_hashes(), audit.CORPUS.read_bytes()
        self.addCleanup(lambda: self.assertEqual(self.before,
            (audit.protected_hashes(), audit.frozen_hashes(), audit.CORPUS.read_bytes())))

    def test_derry_corpus_fail_production_loader_pass_proves_context_mismatch(self):
        row, calls = replay_case('hbo-it-welcome-to-derry-2-3')
        self.assertFalse(row['covered'])
        self.assertEqual(row['primaryFailure'], 'DISCOVERY_MISSED')
        self.assertEqual(row['auditFidelityFinding'], 'IDENTITY_CONTEXT_MISMATCH')
        self.assertEqual(row['currentCoverage'], 'COVERED')
        self.assertTrue(row['historicalExactMatch'])
        self.assertTrue(row['identityContextComparison']['different'])
        self.assertTrue(any('language=es-ES' in url for url in calls))
        self.assertEqual(row['identityContextProvenance']['mode'], 'PRODUCTION_EQUIVALENT_CAPTURE')

    def test_comeback_second_announcement_requires_both_seven_field_replays(self):
        row, _ = replay_case('hbo-the-comeback-3-4')
        self.assertEqual(row['auditFidelityFinding'], 'EQUIVALENT_EVIDENCE')
        self.assertEqual(row['evidenceRelation'], 'FIRST_PARTY_EQUIVALENT')
        self.assertFalse(row['historicalExactMatch'])
        self.assertEqual(row['currentCoverage'], 'COVERED')
        self.assertNotEqual(row['historicalGold']['sourceUrl'], row['currentAuthoritativeFact']['sourceUrl'])
        self.assertTrue(row['evaluationProvenance']['historicalProofValidated'])
        self.assertTrue(row['evaluationProvenance']['currentProofValidated'])

    def test_current_discovery_does_not_require_historical_gold_to_remain_indexed(self):
        row, _ = replay_case('hbo-the-comeback-3-4', includeGoldInIndex=False)
        self.assertEqual(row['primaryFailure'], 'DISCOVERY_MISSED')
        self.assertFalse(row['covered'])
        self.assertTrue(row['productionEquivalentResult']['genericDiscovery'])
        self.assertEqual(row['auditFidelityFinding'], 'EQUIVALENT_EVIDENCE')
        self.assertEqual(row['currentCoverage'], 'COVERED')

    def test_ajlt_direct_pass_never_substitutes_for_generic_miss(self):
        row, calls = replay_case('max-and-just-like-that-2-2')
        self.assertTrue(row['directUrlDiagnostic']['parserCompatible'])
        self.assertFalse(row['productionEquivalentResult']['genericDiscovery'])
        self.assertEqual(row['auditFidelityFinding'], 'REAL_DISCOVERY_FAILURE')
        self.assertEqual(row['currentCoverage'], 'NOT_COVERED')
        self.assertIsNone(row['currentAuthoritativeFact'])
        # Direct diagnostic has its own cache; no known-URL generic seeding.
        self.assertEqual(calls.count(row['canonicalUrl']), 1)

    def test_hacks_later_explicit_final_season_replays_without_parser_changes(self):
        row, _ = replay_case('max-hacks-4-6')
        self.assertEqual(row['auditFidelityFinding'], 'HISTORICAL_GOLD_SUPERSEDED')
        self.assertEqual(row['temporalRelation'], 'SUPERSEDED')
        self.assertEqual(row['evidenceRelation'], 'DIFFERENT_VALID_SUPERSEDING')
        self.assertEqual(row['historicalGold']['nextSeasonNumber'], 4)
        self.assertEqual(row['currentAuthoritativeFact']['nextSeasonNumber'], 5)
        self.assertEqual(row['currentAuthoritativeFact']['status'], 'FINAL_SEASON')
        self.assertFalse(row['historicalExactMatch'])
        self.assertEqual(row['primaryFailure'], 'SEASON_BINDING_FAILED')

    def test_generic_loader_has_no_case_specific_alias_injection(self):
        source = inspect.getsource(audit._fidelity_case_result) + inspect.getsource(audit._identity_context_comparison)
        evaluator = inspect.getsource(fidelity)
        for record in CAPTURE['forensicCases']:
            self.assertNotIn(record['caseId'], source + evaluator)
        self.assertIn('metadata_loader=discovery.tmdb_metadata', source)
        self.assertNotIn('Bienvenidos', source + evaluator)
        for record in CAPTURE['records']:
            self.assertIn('not raw HTTP capture', record['provenance']['method'])

    def test_all_eight_positive_cases_keep_historical_and_current_metrics_separate(self):
        for provider in ('HBO', 'MAX'):
            rows = [replay_case(c['caseId'])[0] for c in historical.PROVIDERS[provider]['cases'] if c['kind']=='positive']
            f = audit._fidelity_rollup(rows)
            self.assertEqual(sum(r['covered'] for r in rows), 2)
            self.assertEqual(f['historicalExactCovered'], 3 if provider=='HBO' else 2)
            self.assertEqual(f['currentAuthoritativeCovered'], 4 if provider=='HBO' else 3)
            self.assertEqual(f['remainingRealFailures'], 0 if provider=='HBO' else 1)

    def test_roi_excludes_fidelity_supersession_equivalence_and_inconclusive(self):
        with patch.object(audit, '_wbd_access_probe', return_value={'status':'LOCAL_INCONCLUSIVE'}), \
             patch.object(audit, '_fx_control_audit', return_value={'status':'LOCAL_INCONCLUSIVE'}):
            report = audit.build_report(audit.load_corpus(), token=None)
        provider = report['providers'][0]
        rows = [replay_case(key)[0] for key in OPTIONS]
        provider['cases'] = rows
        provider['fidelity'] = audit._fidelity_rollup(rows)
        provider['confidence'] = 'HIGH'
        provider['corpusTooSmall'] = False
        provider['measuredPositiveCases'] = 4
        filtered = audit._real_failure_rows([provider])
        self.assertEqual([r['caseId'] for r in filtered[0]['cases']], ['max-and-just-like-that-2-2'])
        self.assertEqual(audit._roi(filtered)[0]['accessibleUncovered'], 1)

    def test_all_ten_providers_receive_additive_fields_offline_never_runner_authority(self):
        with patch.object(audit, '_wbd_access_probe', return_value={'status':'LOCAL_INCONCLUSIVE'}), \
             patch.object(audit, '_fx_control_audit', return_value={'status':'LOCAL_INCONCLUSIVE'}):
            report = audit.build_report(audit.load_corpus(), token=None)
        self.assertEqual(len(report['providers']), 10)
        for p in report['providers']:
            self.assertIn('fidelity', p)
            for row in p['cases']:
                for key in ('historicalGold','currentAuthoritativeFact','historicalExactMatch','currentCoverage',
                    'temporalRelation','evidenceRelation','identityContextMode','auditFidelityFinding','covered','primaryFailure'):
                    self.assertIn(key, row)
                self.assertNotEqual(row['currentCoverage'], 'COVERED')
        self.assertEqual(report['finalCoverageStatus'], 'AWAITING_GITHUB_RUNNER_CONFIRMATION')
        self.assertFalse(report['runnerAuthorityConfirmed'])
        md = audit.render_markdown(report)
        for text in ('Historical Exact Covered','Current Authoritative Covered','Superseded','Equivalent Evidence',
                     'Audit Context Mismatch','Real Failures','HBO discovered 3/4, covered 2/4'):
            self.assertIn(text, md)
        with tempfile.TemporaryDirectory() as directory:
            paths = audit.write_report(report, Path(directory))
            self.assertEqual(len(paths), 3)
            for line in paths[2].read_text(encoding='utf-8').splitlines():
                self.assertIn('historicalGold', json.loads(line))
        self.assertEqual(report['qualityGate']['credentialMatches'], 0)

    def test_captured_metadata_provenance_matches_packaged_identity_fixture(self):
        root = audit.ROOT
        self.assertEqual(hashlib.sha256((root/'official-data/automation/fixtures/v279_hbo_production_evidence.json').read_bytes()).hexdigest(), CAPTURE['priorHboFixtureSha256'])
        for record in CAPTURE['records']:
            provenance = record['provenance']
            self.assertEqual(provenance['baseline'], 'V2.7.15_FORENSICS.md')
            self.assertEqual(provenance['baselineSha256'], historical.RECORDED_FORENSIC_SHA256)
            # The source hash remains documented provenance; an old report is
            # not a runtime dependency of a deterministic regression checkout.
            source = root / provenance['baseline']
            if source.is_file():
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), provenance['baselineSha256'])


if __name__ == '__main__':
    unittest.main()
