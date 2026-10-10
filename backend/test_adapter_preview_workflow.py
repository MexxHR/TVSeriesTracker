"""Keep the AMC/Disney+ acceptance workflow read-only and on the real pipeline."""
import unittest
from pathlib import Path


class AdapterPreviewWorkflowTest(unittest.TestCase):
    def test_manual_preview_uses_monitored_dry_run_and_preserves_live_files(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/validate-amc-disney-adapters.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("  push:", workflow)
        self.assertNotIn("  schedule:", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("monitored.process(tmdb_id, dry_run=True)", workflow)
        self.assertIn("TMDB_API_TOKEN: ${{ secrets.TMDB_API_TOKEN }}", workflow)
        self.assertIn("sha256sum -c", workflow)
        self.assertIn("git diff --exit-code HEAD", workflow)
        self.assertIn("amc-disney-production-adapter-preview", workflow)
        for forbidden in ("git push", "git add", "--promote-monitored", "--process-discovered",
                          "OFFICIAL_DATA_PROMOTION_ENABLED", "contents: write", "continue-on-error"):
            self.assertNotIn(forbidden, workflow)


if __name__ == "__main__":
    unittest.main()
