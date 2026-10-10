"""The Disney+ candidate probe must report production results without writes."""
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import disney_candidate_probe as probe
import promotion


META = {"tmdbId": 138503, "title": "Your Friendly Neighborhood Spider-Man",
        "networks": [{"name": "Disney+"}], "productionCompanies": [],
        "homepage": None, "originCountry": []}


class DisneyCandidateProbeTest(unittest.TestCase):
    def test_diagnostic_main_writes_artifact_and_green_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            preview = {"schemaVersion": 1, "reportType": "disney_plus_candidate_read_only_preview",
                       "metadata": META, "routing": {"provider": "DISNEY_PLUS", "routeSignals": ["networks:Disney+"]},
                       "monitoredPreview": {"pipelineState": "NO_VERIFIED_FACTS", "parserResult": "no_facts",
                                            "validationResult": "not_applicable"},
                       "independentReconstruction": {"result": "not_applicable"}}
            summary = Path(directory) / "summary.md"
            with patch.object(probe, "probe", return_value=preview), \
                 patch.object(probe, "protected_hashes", return_value={"protected": "unchanged"}), \
                 patch.object(probe.discovery, "local_tmdb_token", return_value=""), \
                 patch.dict(os.environ, {"RUNNER_TEMP": directory, "GITHUB_STEP_SUMMARY": str(summary)}):
                self.assertEqual(probe.main(), 0)
            artifact = json.loads((Path(directory) / "disney-plus-candidate-preview.json").read_text())
            self.assertTrue(artifact["protectedFileIntegrity"]["unchanged"])
            self.assertIn("Protected files unchanged: yes", summary.read_text())

    def test_non_disney_route_stops_before_factual_processing(self):
        metadata = {**META, "networks": [{"name": "Netflix"}]}

        def forbidden(*args, **kwargs):
            self.fail("Non-Disney candidate reached monitored processing")

        result = probe.probe(metadata, processor=forbidden, discoverer=forbidden)
        self.assertEqual(result["routing"]["provider"], "NETFLIX")
        self.assertTrue(result["routing"]["factualProcessingStopped"])
        self.assertIsNone(result["monitoredPreview"])

    def test_diagnostic_no_facts_is_successful_dry_run(self):
        proposed = {"tmdbId": 138503, "discoveryState": "NO_VERIFIED_FACTS"}

        def discover(metadata, trusted):
            self.assertIs(metadata, META)
            return {"result": "candidate_found", "candidateUrls": ["https://press.disneyplus.com/example"],
                    "candidates": [], "rejectedCandidates": [{"url": "https://press.disneyplus.com/other"}]}

        def process(tmdb_id, *, dry_run, metadata, discoverer):
            self.assertEqual(tmdb_id, 138503)
            self.assertTrue(dry_run)
            discovered = discoverer(metadata, {"series": []})
            self.assertEqual(discovered["result"], "candidate_found")
            return {"tmdbId": tmdb_id, "provider": "DISNEY_PLUS", "dryRun": True,
                    "pipelineState": "NO_VERIFIED_FACTS", "validationResult": "not_applicable",
                    "registryChange": "would_add", "proposedRecord": proposed}

        result = probe.probe(META, processor=process, discoverer=discover)
        self.assertEqual(result["discoveryDiagnostics"]["rejectedCandidates"][0]["url"],
                         "https://press.disneyplus.com/other")
        self.assertEqual(result["proposedMonitoredRecord"], proposed)
        self.assertEqual(result["independentReconstruction"]["result"], "not_applicable")
        self.assertFalse(result["recommendedForLiveE2E"])

    def test_verified_facts_use_independent_reconstruction(self):
        facts = {"status": "RENEWED", "nextSeasonNumber": 2, "releaseDate": None,
                 "releaseYear": None, "sourceName": "Disney+ Press",
                 "sourceUrl": "https://press.disneyplus.com/example", "announcementDate": None}
        row = {"tmdbId": 138503, "lastChecked": "2026-10-10", "verifiedFacts": facts}

        def process(tmdb_id, *, dry_run, metadata, discoverer):
            discoverer(metadata, {"series": []})
            return {"tmdbId": tmdb_id, "provider": "DISNEY_PLUS", "dryRun": True,
                    "pipelineState": "VERIFIED_FACTS", "validationResult": "passed",
                    "registryChange": "would_add", "verifiedFacts": facts,
                    "proposedRecord": row}

        with patch.object(promotion, "_validate_evidence", return_value=[{"factType": "LIFECYCLE"}]) as validate, \
             patch.object(promotion, "_facts_summary", return_value=facts) as rebuild:
            result = probe.probe(META, processor=process,
                                 discoverer=lambda *_: {"result": "candidate_found"})
        validate.assert_called_once()
        rebuild.assert_called_once()
        self.assertEqual(result["independentReconstruction"]["facts"], facts)
        self.assertTrue(result["recommendedForLiveE2E"])

    def test_independent_mismatch_is_reported_as_failure(self):
        row = {"tmdbId": 138503, "lastChecked": "2026-10-10", "verifiedFacts": {}}

        def process(tmdb_id, *, dry_run, metadata, discoverer):
            discoverer(metadata, {"series": []})
            return {"tmdbId": tmdb_id, "dryRun": True, "provider": "DISNEY_PLUS", "pipelineState": "VERIFIED_FACTS",
                    "validationResult": "passed", "proposedRecord": row}

        mismatch = promotion.EvidenceMismatch(["status"], {"status": "RENEWED"},
                                               {"status": "CANCELED"})
        with patch.object(promotion, "_validate_evidence", side_effect=mismatch):
            result = probe.probe(META, processor=process,
                                 discoverer=lambda *_: {"result": "candidate_found"})
        self.assertEqual(result["independentReconstruction"]["result"], "failed")
        self.assertEqual(result["independentReconstruction"]["mismatch"]["fields"], ["status"])
        self.assertFalse(result["recommendedForLiveE2E"])


if __name__ == "__main__":
    unittest.main()
