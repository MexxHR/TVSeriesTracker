"""Static safety checks for the manual FX/Hulu qualification workflow."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FxHuluQualificationWorkflowTest(unittest.TestCase):
    def test_workflow_is_manual_read_only_and_artifact_bound(self):
        workflow = (ROOT / ".github/workflows/qualify-fx-hulu.yml").read_text(encoding="utf-8")
        trigger = workflow.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
        permissions = workflow.split("permissions:\n", 1)[1].split("\njobs:", 1)[0]
        self.assertEqual(trigger.strip(), "workflow_dispatch:")
        self.assertEqual(permissions.strip(), "contents: read")
        self.assertIn("name: FX + Hulu Provider Qualification", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("test_*.py", workflow)
        self.assertIn("fx_hulu_qualification.py --output", workflow)
        self.assertIn("set -euo pipefail", workflow)
        self.assertIn("sha256sum -c", workflow)
        self.assertIn("official-data/official_series_data.json", workflow)
        self.assertIn("official-data/history/changes.jsonl", workflow)
        self.assertIn("official-data/sources.json", workflow)
        self.assertIn("official-data/monitored_series.json", workflow)
        self.assertIn("official-data/history/monitored_changes.jsonl", workflow)
        self.assertIn("git diff --exit-code HEAD", workflow)
        self.assertIn("git ls-files --others --exclude-standard", workflow)
        self.assertGreaterEqual(workflow.count("if: always()"), 2)
        self.assertIn("$GITHUB_STEP_SUMMARY", workflow)
        self.assertIn("name: fx-hulu-provider-qualification", workflow)
        self.assertIn("fx-hulu-provider-qualification.json", workflow)
        for forbidden in ("  push:", "  schedule:", "contents: write", "git add", "git commit",
                          "git push", "--process-discovered", "--promote-monitored", "secrets.",
                          "OFFICIAL_DATA_LIVE_ENABLED", "OFFICIAL_DATA_PROMOTION_ENABLED"):
            self.assertNotIn(forbidden, workflow)

    def test_helper_never_uses_production_writer_or_claims_activation(self):
        helper = (ROOT / "official-data/automation/fx_hulu_qualification.py").read_text(encoding="utf-8")
        production_registry = (ROOT / "official-data/discovery_providers.json").read_text(encoding="utf-8")
        self.assertNotIn('"FX"', production_registry)
        self.assertNotIn('"HULU"', production_registry)
        self.assertIn('"fxReadyForProductionActivation": False', helper)
        self.assertIn('"huluReadyForProductionActivation": False', helper)
        self.assertIn("promotion._validate_evidence", helper)
        self.assertIn("promotion._facts_summary", helper)
        self.assertIn("monitored.FACT_FIELDS", helper)
        self.assertIn("tmdbId\": case_config.get(\"tmdbId\")", helper)
        self.assertNotIn("monitored.process(", helper)
        self.assertNotIn("promotion.promote(", helper)
        self.assertNotIn("update.publish(", helper)
        self.assertNotIn("monitored.publish(", helper)
        self.assertIn("internalSyntheticKeyUsed", helper)
        self.assertNotIn('"discoveryState": "VERIFIED_FACTS"', helper)
        self.assertIn("PROTECTED_FILES", helper)
        self.assertIn("hashlib.sha256", helper)


if __name__ == "__main__":
    unittest.main()
