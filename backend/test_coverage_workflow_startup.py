"""Startup regression checks for the fixed, conventional audit workflow layout.

These scope checks supplement YAML parsing; they are not a general YAML parser
or a replacement for actionlint. Context rules: GitHub Actions contexts reference.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/provider-coverage-audit.yml'


def validate_expression_scopes(source):
    """Check every expression in this workflow's job and step sections."""
    before_steps, steps = source.split('    steps:\n', 1)
    for expression in re.findall(r'\$\{\{(.*?)\}\}', before_steps):
        contexts = re.findall(r'\b([a-zA-Z_]+)\.', expression)
        # No matrix, needs, env, steps or runner context is needed here.
        if any(context not in {'github', 'secrets', 'inputs', 'vars'} for context in contexts):
            raise ValueError('Unsupported pre-runner expression: ' + expression.strip())
    for expression in re.findall(r'\$\{\{(.*?)\}\}', steps):
        # The only expressions in this narrow workflow are a step env secret
        # and runner.temp in upload-artifact's step-level with.path.
        if expression.strip() not in {'secrets.TMDB_API_TOKEN', 'runner.temp'}:
            raise ValueError('Unreviewed step expression: ' + expression.strip())
    return before_steps, steps


class CoverageWorkflowStartupTests(unittest.TestCase):
    def setUp(self):
        self.source = WORKFLOW.read_text(encoding='utf-8')

    def test_all_expression_contexts_are_reviewed(self):
        before, steps = validate_expression_scopes(self.source)
        self.assertNotIn('${{', before)
        self.assertEqual(re.findall(r'\$\{\{\s*(.*?)\s*\}\}', steps),
                         ['secrets.TMDB_API_TOKEN', 'runner.temp', 'runner.temp', 'runner.temp'])
        audit = steps.split('- name: Audit frozen provider coverage', 1)[1].split('- name:', 1)[0]
        self.assertIn('TMDB_API_TOKEN: ${{ secrets.TMDB_API_TOKEN }}', audit)
        upload = steps.split('- uses: actions/upload-artifact@v4', 1)[1]
        self.assertEqual(upload.count('${{ runner.temp }}'), 3)

    def test_original_job_env_startup_failure_is_rejected(self):
        broken = self.source.replace("      PYTHONDONTWRITEBYTECODE: '1'",
            "      PYTHONDONTWRITEBYTECODE: '1'\n"
            '      TEMP: ${{ runner.temp }}/coverage-tmp\n'
            '      TMP: ${{ runner.temp }}/coverage-tmp')
        with self.assertRaisesRegex(ValueError, 'pre-runner.*runner.temp'):
            validate_expression_scopes(broken)

    def test_unreviewed_pre_runner_contexts_require_review(self):
        for context in ('runner.os', 'steps.prepare.outputs.path', 'env.TEMP', 'matrix.os', 'needs.build.outputs.path'):
            with self.subTest(context=context):
                broken = self.source.replace('    steps:\n', '      PATH: ${{ ' + context + ' }}\n    steps:\n')
                with self.assertRaises(ValueError):
                    validate_expression_scopes(broken)

    def test_runtime_temp_initialization_precedes_every_other_step(self):
        steps = self.source.split('    steps:\n', 1)[1]
        self.assertTrue(steps.startswith('      - name: Initialize runner temporary paths\n'))
        setup = steps.split('      - uses:', 1)[0]
        self.assertIn('mkdir -p "$RUNNER_TEMP/coverage-tmp"', setup)
        for name in ('TEMP', 'TMP'):
            self.assertIn(f"printf '{name}=%s/coverage-tmp\\n' \"$RUNNER_TEMP\" >> \"$GITHUB_ENV\"", setup)
        self.assertNotIn('/home/runner/', self.source)

    def test_producer_summary_and_upload_share_artifact_directory(self):
        self.assertIn('--output-dir "$RUNNER_TEMP/provider-coverage-audit"', self.source)
        self.assertIn('cat "$RUNNER_TEMP/provider-coverage-audit/provider-coverage-audit.md"', self.source)
        upload = self.source.split('- uses: actions/upload-artifact@v4', 1)[1]
        harness = (ROOT / 'official-data/automation/provider_coverage_audit.py').read_text(encoding='utf-8')
        for name in ('provider-coverage-audit.json', 'provider-coverage-audit.md', 'provider-coverage-cases.jsonl'):
            self.assertIn('${{ runner.temp }}/provider-coverage-audit/' + name, upload)
            self.assertIn('output_dir / "' + name + '"', harness)
        self.assertIn('name: provider-coverage-audit', upload)

    def test_runner_authority_is_requested_only_by_actual_audit_step(self):
        self.assertEqual(self.source.count('--execution-mode runner-confirmation'), 1)
        audit = self.source.split('- name: Audit frozen provider coverage', 1)[1].split('- name:', 1)[0]
        self.assertIn('--execution-mode runner-confirmation', audit)
        regression = self.source.split('- name: Official Data regression tests', 1)[1].split('- name:', 1)[0]
        self.assertNotIn('--execution-mode', regression)
        self.assertNotIn('continue-on-error', self.source)
        self.assertLess(self.source.index('Official Data regression tests'), self.source.index('Validate independent audit corpus'))
        self.assertLess(self.source.index('Validate independent audit corpus'), self.source.index('Audit frozen provider coverage'))

    def test_manual_read_only_and_protected_guards_preserved(self):
        triggers = self.source.split('on:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertEqual(triggers.strip(), 'workflow_dispatch:')
        permissions = self.source.split('permissions:\n', 1)[1].split('\njobs:', 1)[0]
        self.assertEqual(permissions.strip(), 'contents: read')
        self.assertIn('persist-credentials: false', self.source)
        self.assertIn('Snapshot protected live files before tests', self.source)
        self.assertIn('Verify protected live files and checkout\n        if: always()', self.source)
        self.assertIn("subprocess.run(['git', 'diff', '--quiet', 'HEAD'])", self.source)
        for forbidden in ('git push', 'git commit', 'gh pr', 'contents: write', 'promotion.main(', '/requests'):
            self.assertNotIn(forbidden, self.source)

    def test_embedded_python_blocks_compile(self):
        blocks = re.findall(r"          python - <<'PY'\n(.*?)          PY", self.source, re.S)
        self.assertEqual(len(blocks), 2)
        for index, block in enumerate(blocks):
            body = '\n'.join(line[10:] if line.startswith('          ') else line for line in block.splitlines())
            compile(body, f'audit-workflow-block-{index}', 'exec')


if __name__ == '__main__':
    unittest.main()
