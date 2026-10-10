"""The Disney+ candidate probe must report production results without writes."""
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import ANY, patch

sys.path.insert(0, str(Path(__file__).parent))
import disney_candidate_probe as probe
import promotion


META = {"tmdbId": 138503, "title": "Your Friendly Neighborhood Spider-Man",
        "networks": [{"name": "Disney+"}], "productionCompanies": [],
        "homepage": None, "originCountry": []}


class DisneyCandidateProbeTest(unittest.TestCase):
    def test_positive_decimal_input_and_rejection_of_untrusted_text(self):
        self.assertEqual(probe.parse_tmdb_id("138503"), 138503)
        self.assertEqual(probe.parse_tmdb_id("246810"), 246810)
        self.assertEqual(probe.parse_tmdb_id("00042"), 42)
        for raw in ("", "0", "000", "-1", "abc", "138503; echo hacked",
                    "$(whoami)", "138503 && env", "  ", " 138503", "138503 ",
                    "１２３", "9" * 19):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    probe.parse_tmdb_id(raw)

    def test_other_id_flows_to_real_metadata_path(self):
        other = {**META, "tmdbId": 246810, "title": "Another Disney Series"}
        processed = []

        def process(tmdb_id, *, dry_run, metadata, discoverer):
            processed.append((tmdb_id, dry_run, metadata))
            discoverer(metadata, {"series": []})
            return {"tmdbId": tmdb_id, "dryRun": True, "provider": "DISNEY_PLUS",
                    "pipelineState": "DISCOVERY_INSUFFICIENT", "parserResult": "skipped",
                    "validationResult": "not_applicable", "registryChange": "would_add",
                    "proposedRecord": {"tmdbId": tmdb_id}}

        with patch.object(probe.discovery, "tmdb_metadata", return_value=other) as fetch:
            result = probe.probe(246810, processor=process,
                                 discoverer=lambda *_: {"result": "insufficient_evidence"})
        fetch.assert_called_once_with(246810, ANY)
        self.assertEqual(processed, [(246810, True, other)])
        self.assertEqual(result["candidate"], {"tmdbId": 246810})
        self.assertEqual(result["metadata"]["title"], "Another Disney Series")

    def test_metadata_unavailable_is_a_safe_diagnostic(self):
        with patch.object(probe.discovery, "tmdb_metadata", side_effect=ValueError("Unavailable")):
            result = probe.probe(246810)
        self.assertEqual(result["monitoredPreview"]["pipelineState"], "METADATA_UNAVAILABLE")
        self.assertIsNone(result["metadata"])
        self.assertFalse(result["recommendedForLiveE2E"])

    def test_diagnostic_main_writes_artifact_and_green_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            preview = {"schemaVersion": 1, "reportType": "disney_plus_candidate_read_only_preview",
                       "candidate": {"tmdbId": 138503},
                       "metadata": META, "routing": {"provider": "DISNEY_PLUS", "routeSignals": ["networks:Disney+"]},
                       "monitoredPreview": {"pipelineState": "NO_VERIFIED_FACTS", "parserResult": "no_facts",
                                            "validationResult": "not_applicable"},
                       "independentReconstruction": {"result": "not_applicable"}}
            summary = Path(directory) / "summary.md"
            with patch.object(probe, "probe", return_value=preview), \
                 patch.object(probe, "protected_hashes", return_value={"protected": "unchanged"}), \
                 patch.object(probe.discovery, "local_tmdb_token", return_value=""), \
                 patch.dict(os.environ, {"RUNNER_TEMP": directory, "GITHUB_STEP_SUMMARY": str(summary)}):
                self.assertEqual(probe.main("000138503"), 0)
            artifact = json.loads((Path(directory) / "disney-plus-candidate-138503-preview.json").read_text())
            self.assertTrue(artifact["protectedFileIntegrity"]["unchanged"])
            self.assertEqual(artifact["candidate"], {"tmdbId": 138503})
            self.assertIn("Protected files unchanged: yes", summary.read_text())
            self.assertIn("TMDB ID: 138503", summary.read_text())
            self.assertEqual(list(Path(directory).glob("*.json")),
                             [Path(directory) / "disney-plus-candidate-138503-preview.json"])

    def test_invalid_id_cannot_create_an_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"RUNNER_TEMP": directory}):
                with self.assertRaises(ValueError):
                    probe.main("138503; echo hacked")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_protected_file_mutation_fails_after_artifact_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            hashes = iter([{"protected": "before"}, {"protected": "after"}])
            preview = {"candidate": {"tmdbId": 246810}, "metadata": {"title": "Test"},
                       "routing": {"provider": None}, "monitoredPreview": None,
                       "independentReconstruction": {"result": "not_applicable"}}
            with patch.object(probe, "probe", return_value=preview), \
                 patch.object(probe, "protected_hashes", side_effect=lambda: next(hashes)), \
                 patch.object(probe.discovery, "local_tmdb_token", return_value=""), \
                 patch.dict(os.environ, {"RUNNER_TEMP": directory, "GITHUB_STEP_SUMMARY": str(summary)}):
                with self.assertRaises(SystemExit):
                    probe.main("246810")
            artifact = json.loads((Path(directory) / "disney-plus-candidate-246810-preview.json").read_text())
            self.assertFalse(artifact["protectedFileIntegrity"]["unchanged"])

    def test_non_disney_route_stops_before_factual_processing(self):
        metadata = {**META, "networks": [{"name": "Netflix"}]}

        def forbidden(*args, **kwargs):
            self.fail("Non-Disney candidate reached monitored processing")

        result = probe.probe(138503, metadata, processor=forbidden, discoverer=forbidden)
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

        result = probe.probe(138503, META, processor=process, discoverer=discover)
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
                    "pipelineState": "VERIFIED_FACTS", "parserResult": "facts", "validationResult": "passed",
                    "registryChange": "would_add", "verifiedFacts": facts,
                    "proposedRecord": row}

        with patch.object(promotion, "_validate_evidence", return_value=[{"factType": "LIFECYCLE"}]) as validate, \
             patch.object(promotion, "_facts_summary", return_value=facts) as rebuild:
            result = probe.probe(138503, META, processor=process,
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
                    "parserResult": "facts",
                    "validationResult": "passed", "proposedRecord": row}

        mismatch = promotion.EvidenceMismatch(["status"], {"status": "RENEWED"},
                                               {"status": "CANCELED"})
        with patch.object(promotion, "_validate_evidence", side_effect=mismatch):
            result = probe.probe(138503, META, processor=process,
                                 discoverer=lambda *_: {"result": "candidate_found"})
        self.assertEqual(result["independentReconstruction"]["result"], "failed")
        self.assertEqual(result["independentReconstruction"]["mismatch"]["fields"], ["status"])
        self.assertFalse(result["recommendedForLiveE2E"])


if __name__ == "__main__":
    unittest.main()
