"""Keep the Disney+ candidate workflow manual, read-only and credential-safe."""
from pathlib import Path
import unittest


class DisneyCandidateWorkflowTest(unittest.TestCase):
    def test_workflow_uses_read_only_production_probe(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/validate-disney-candidate.yml').read_text(encoding='utf-8')
        helper = (root / 'official-data/automation/disney_candidate_probe.py').read_text(encoding='utf-8')
        self.assertIn('name: Validate Disney+ candidate (read-only)', workflow)
        self.assertIn('workflow_dispatch:', workflow)
        self.assertIn('tmdb_id:', workflow)
        self.assertIn('required: true', workflow)
        self.assertIn('type: string', workflow)
        self.assertIn('permissions:\n  contents: read', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('RAW_TMDB_ID: ${{ inputs.tmdb_id }}', workflow)
        self.assertIn('disney_candidate_probe.py --validate-id', workflow)
        self.assertIn('TMDB_ID: ${{ steps.candidate.outputs.tmdb_id }}', workflow)
        self.assertIn('TMDB_API_TOKEN: ${{ secrets.TMDB_API_TOKEN }}', workflow)
        self.assertIn('python official-data/automation/disney_candidate_probe.py', workflow)
        self.assertIn('sha256sum -c', workflow)
        self.assertIn('git diff --exit-code HEAD', workflow)
        self.assertIn('disney-plus-candidate-${{ steps.candidate.outputs.tmdb_id }}-preview.json', workflow)
        self.assertIn('parse_tmdb_id(raw_tmdb_id)', helper)
        self.assertIn('monitored.process', helper)
        self.assertIn('dry_run=True', helper)
        self.assertIn('promotion._validate_evidence', helper)
        self.assertIn('promotion._facts_summary', helper)
        self.assertNotIn('138503', workflow)
        self.assertNotIn('138503', helper)
        self.assertNotIn('Your Friendly Neighborhood Spider-Man', workflow)
        self.assertNotIn('Your Friendly Neighborhood Spider-Man', helper)
        for forbidden in ('  push:', '  schedule:', 'contents: write', 'git push',
                          'git add', '--promote-monitored', 'continue-on-error'):
            self.assertNotIn(forbidden, workflow)

    def test_android_version_is_unchanged(self):
        root = Path(__file__).resolve().parents[1]
        gradle = (root / 'app/build.gradle.kts').read_text(encoding='utf-8')
        self.assertIn('versionCode = 28', gradle)
        self.assertIn('versionName = "2.7.3"', gradle)


if __name__ == '__main__':
    unittest.main()
