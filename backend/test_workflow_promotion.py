"""Static guardrails for the Phase 2B.2 GitHub Actions write boundary."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class PromotionWorkflowBoundaryTests(unittest.TestCase):
    def test_all_data_writers_share_non_cancelling_concurrency(self):
        for filename in (
            "process-series-request.yml",
            "process-discovered-series.yml",
            "update-official-data.yml",
        ):
            with self.subTest(filename=filename):
                source = (WORKFLOWS / filename).read_text(encoding="utf-8")
                self.assertRegex(
                    source,
                    r"(?m)^  group: official-data-writes$",
                )
                self.assertRegex(source, r"(?m)^  cancel-in-progress: false$")

    def test_request_promotes_only_after_successful_monitored_processing(self):
        source = (WORKFLOWS / "process-series-request.yml").read_text(
            encoding="utf-8"
        )
        process = source.index("--process-discovered")
        promote = source.index("--promote-monitored")
        allowlist = source.index("Enforce changed-file allowlist")
        commit = source.index("Commit permitted monitored and production data")
        self.assertLess(process, promote)
        self.assertLess(promote, allowlist)
        self.assertLess(allowlist, commit)
        self.assertNotRegex(source[promote:allowlist], r"continue-on-error:\s*true")
        self.assertIn("if: ${{ vars.OFFICIAL_DATA_PROMOTION_ENABLED == 'true' }}", source)

    def test_request_workflow_has_an_exact_four_path_allowlist(self):
        source = (WORKFLOWS / "process-series-request.yml").read_text(
            encoding="utf-8"
        )
        section = source.split("allowed = {", 1)[1].split("}", 1)[0]
        allowed = set(re.findall(r'"([^\"]+)"', section))
        self.assertEqual(
            allowed,
            {
                "official-data/monitored_series.json",
                "official-data/history/monitored_changes.jsonl",
                "official-data/official_series_data.json",
                "official-data/history/changes.jsonl",
            },
        )
        self.assertNotIn("official-data/sources.json", allowed)
        self.assertIn('"git", "diff", "--name-only", "--no-renames", "-z", "HEAD"', source)
        self.assertIn('"git", "ls-files", "--others", "--exclude-standard", "-z"', source)
        self.assertIn('Production data changed while promotion is disabled', source)

    def test_preview_workflow_is_read_only_and_uses_dry_run(self):
        source = (WORKFLOWS / "preview-monitored-promotion.yml").read_text(encoding="utf-8")
        self.assertIn("contents: read", source)
        self.assertIn("persist-credentials: false", source)
        self.assertIn('python official-data/automation/update.py --promote-monitored "$TMDB_ID" --dry-run', source)
        self.assertIn("git diff --exit-code HEAD", source)
        self.assertIn('PYTHONDONTWRITEBYTECODE: \'1\'', source)
        self.assertIn('git ls-files --others --exclude-standard', source)
        self.assertNotIn("git push", source)


if __name__ == "__main__":
    unittest.main()
