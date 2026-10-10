"""Static guardrails for the Phase 2B.2 GitHub Actions write boundary."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class PromotionWorkflowBoundaryTests(unittest.TestCase):
    def test_provider_discovery_qualification_workflow_is_read_only(self):
        source = (WORKFLOWS / "provider-discovery-qualification.yml").read_text(encoding="utf-8")
        triggers = source.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(triggers.strip(), "workflow_dispatch:")
        permissions = source.split("permissions:\n", 1)[1].split("\njobs:", 1)[0]
        self.assertEqual(permissions.strip(), "contents: read")
        self.assertIn("persist-credentials: false", source)
        self.assertIn("PYTHONDONTWRITEBYTECODE: '1'", source)
        self.assertLess(source.index("Official Data regression tests"),
                        source.index("Qualify candidate providers"))
        self.assertIn("provider_audit.py --qualify --all", source)
        self.assertIn("git diff --exit-code HEAD", source)
        self.assertIn("git ls-files --others --exclude-standard", source)
        self.assertNotIn("secrets.", source)
        self.assertNotIn("git push", source)
        self.assertNotIn("contents: write", source)

    def test_provider_coverage_audit_workflow_is_read_only(self):
        source = (WORKFLOWS / "provider-coverage-audit.yml").read_text(encoding="utf-8")
        triggers = source.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(triggers.strip(), "workflow_dispatch:")
        permissions = source.split("permissions:\n", 1)[1].split("\njobs:", 1)[0]
        self.assertEqual(permissions.strip(), "contents: read")
        self.assertIn("persist-credentials: false", source)
        self.assertIn("PYTHONDONTWRITEBYTECODE: '1'", source)
        self.assertLess(source.index("Official Data regression tests"),
                        source.index("Audit official provider coverage"))
        self.assertIn("provider_audit.py --all", source)
        self.assertIn("git diff --exit-code HEAD", source)
        self.assertIn("git ls-files --others --exclude-standard", source)
        self.assertNotIn("secrets.", source)
        self.assertNotIn("git push", source)
        self.assertNotIn("contents: write", source)

    def test_all_data_writers_share_non_cancelling_concurrency(self):
        for filename in (
            "process-series-request.yml",
            "process-discovered-series.yml",
            "update-official-data.yml",
            "promote-monitored-series.yml",
        ):
            with self.subTest(filename=filename):
                source = (WORKFLOWS / filename).read_text(encoding="utf-8")
                self.assertRegex(
                    source,
                    r"(?m)^  group: official-data-writes$",
                )
                self.assertRegex(source, r"(?m)^  cancel-in-progress: false$")

    def test_request_promotes_only_after_monitored_commit_and_exact_gate(self):
        source = (WORKFLOWS / "process-series-request.yml").read_text(
            encoding="utf-8"
        )
        process = source.index("--process-discovered")
        promote = source.index("--promote-monitored")
        stage_guard = source.index("automatic_promotion.py verify-stage")
        monitored_commit = source.index("Commit monitored staging only when changed")
        production_guard = source.index("manual_promotion_guard.py verify-auto")
        production_commit = source.index("Commit only a verified production promotion")
        self.assertLess(process, stage_guard)
        self.assertLess(stage_guard, monitored_commit)
        self.assertLess(monitored_commit, promote)
        self.assertLess(promote, production_guard)
        self.assertLess(production_guard, production_commit)
        self.assertIn("needs.monitored_update.outputs.promotion_enabled == 'true'", source)
        self.assertIn("needs.monitored_update.outputs.monitored_state == 'VERIFIED_FACTS'", source)
        self.assertNotRegex(source[process:production_commit], r"continue-on-error:\s*true")

    def test_request_workflow_separates_stage_allowlists_and_current_main(self):
        source = (WORKFLOWS / "process-series-request.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("automatic_promotion.py verify-stage", source)
        self.assertIn("manual_promotion_guard.py verify-auto", source)
        self.assertIn("git add -- official-data/monitored_series.json official-data/history/monitored_changes.jsonl", source)
        self.assertIn("git add -- official-data/official_series_data.json official-data/history/changes.jsonl", source)
        self.assertNotIn("git add -- official-data/sources.json", source)
        self.assertIn("REF: ${{ github.ref }}", source)
        self.assertEqual(source.count("ref: refs/heads/main"), 2)
        self.assertIn('test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD"', source)
        self.assertIn("needs.monitored_update.outputs.staged_head", source)
        self.assertIn("PYTHONDONTWRITEBYTECODE: '1'", source)
        self.assertNotIn("git push --force", source)

    def test_preview_workflow_is_read_only_and_uses_dry_run(self):
        source = (WORKFLOWS / "preview-monitored-promotion.yml").read_text(encoding="utf-8")
        self.assertIn("contents: read", source)
        self.assertIn("persist-credentials: false", source)
        self.assertIn('python official-data/automation/update.py --promote-monitored "$TMDB_ID" --dry-run', source)
        self.assertIn("git diff --exit-code HEAD", source)
        self.assertIn('PYTHONDONTWRITEBYTECODE: \'1\'', source)
        self.assertIn('git ls-files --others --exclude-standard', source)
        self.assertNotIn("git push", source)

    def test_manual_promotion_is_human_dispatched_and_narrowly_permissioned(self):
        source = (WORKFLOWS / "promote-monitored-series.yml").read_text(encoding="utf-8")
        triggers = source.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("workflow_dispatch:", triggers)
        self.assertNotIn("schedule:", triggers)
        self.assertNotIn("push:", triggers)
        self.assertRegex(triggers, r"tmdb_id:\n(?:.*\n)*?        required: true")
        permissions = source.split("permissions:\n", 1)[1].split("\nconcurrency:", 1)[0]
        self.assertEqual(permissions.strip(), "contents: write")
        self.assertIn("REF: ${{ github.ref }}", source)
        self.assertIn("test \"$REF\" = refs/heads/main", source)
        self.assertIn("ref: refs/heads/main", source)
        self.assertIn("PYTHONDONTWRITEBYTECODE: '1'", source)
        self.assertIn("manual_promotion_guard.py validate-id", source)
        self.assertIn("manual_promotion_guard.py preflight", source)
        self.assertIn("manual_promotion_guard.py verify", source)
        self.assertIn('python official-data/automation/update.py --promote-monitored "$TMDB_ID"', source)
        self.assertNotIn("--dry-run", source)
        self.assertNotIn("OFFICIAL_DATA_PROMOTION_ENABLED", source)

    def test_manual_promotion_commits_only_after_verified_real_change(self):
        source = (WORKFLOWS / "promote-monitored-series.yml").read_text(encoding="utf-8")
        self.assertIn("if: ${{ steps.verify.outputs.commit == 'true' }}", source)
        self.assertIn("git add -- official-data/official_series_data.json official-data/history/changes.jsonl", source)
        self.assertIn("git push origin HEAD:main", source)
        self.assertNotIn("official-data/monitored_series.json official-data/history/monitored_changes.jsonl", source)
        guard = (ROOT / "official-data" / "automation" / "manual_promotion_guard.py").read_text(encoding="utf-8")
        self.assertIn("if state == \"NO_CHANGE\":", guard)
        self.assertIn("if state != \"PROMOTED\":", guard)
        self.assertIn('WRITABLE = set(PROTECTED[:2])', guard)
        self.assertIn('different != {tmdb_id}', guard)
        self.assertIn('changed - WRITABLE', guard)
        automatic = (WORKFLOWS / "process-series-request.yml").read_text(encoding="utf-8")
        self.assertIn("RAW_PROMOTION_GATE: ${{ vars.OFFICIAL_DATA_PROMOTION_ENABLED }}", automatic)
        self.assertIn("needs.monitored_update.outputs.promotion_enabled == 'true'", automatic)


if __name__ == "__main__":
    unittest.main()
