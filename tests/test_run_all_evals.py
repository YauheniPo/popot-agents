"""The manual launcher runs both eval suites and retains a failed capture."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RunAllEvalTests(unittest.TestCase):
    def run_launcher(self, fail=False, extra=(), matrix=False, mutate=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'evals').mkdir()
            shutil.copyfile(ROOT / 'evals/run-all.sh', root / 'evals/run-all.sh')
            if (ROOT / 'evals/publish.sh').exists():
                shutil.copyfile(ROOT / 'evals/publish.sh', root / 'evals/publish.sh')
            shutil.copyfile(ROOT / 'compose.sh', root / 'compose.sh')
            shutil.copyfile(ROOT / 'evals/config.json', root / 'evals/config.json')
            tools = root / 'bin'
            tools.mkdir()
            docker = tools / 'docker'
            docker.write_text('#!/usr/bin/env bash\n'
                              'printf "%s\\n" "$*" >> "$EVAL_TEST_LOG"\n'
                              'if [[ "$1" == cp && "$2" == *:* && "$3" != *:* && "$3" != *.json ]]; then mkdir -p "$3"; echo "{}" > "$3/report.json"; echo "{}" > "$3/config.json"; fi\n'
                              'if [[ "$*" == *"-m evals.baseline"* && "${EVAL_TEST_FAIL:-}" == 1 ]]; then exit 7; fi\n'
                              'if [[ "$*" == *"-m evals.baseline"* && "${EVAL_TEST_MUTATE:-}" == 1 ]]; then printf "%10000s" " " > "$EVAL_TEST_SCRIPT"; printf "\\\"" >> "$EVAL_TEST_SCRIPT"; fi\n'
                              'if [[ "$*" == *"-gateway.mjs"* && "$1" == exec ]]; then echo "[]"; fi\n')
            docker.chmod(0o755)
            git = tools / 'git'
            git.write_text('#!/usr/bin/env bash\nif [[ "$1" == rev-parse ]]; then echo "1234567890123456789012345678901234567890"; fi\n')
            git.chmod(0o755)
            python = tools / 'python3'
            python.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$EVAL_TEST_LOG"\n')
            python.chmod(0o755)
            log = root / 'commands.log'
            env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
                   'EVAL_TEST_LOG': str(log), 'EVAL_TEST_FAIL': '1' if fail else '',
                   'EVAL_TEST_MUTATE': '1' if mutate else '', 'EVAL_TEST_SCRIPT': str(root / 'evals/run-all.sh')}
            if matrix:
                env.update(POPOT_EVAL_SKIP_RUNTIME='1', POPOT_EVAL_BASELINE='1')
            result = subprocess.run(['bash', str(root / 'evals/run-all.sh'), 'fixture', *extra],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=10)
            return result, log.read_text()

    def test_both_suites_gateway_and_phoenix_import_run_in_order(self):
        result, log = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('-m evals.runtime', log)
        self.assertIn('-m evals.baseline', log)
        self.assertIn('-gateway.mjs /tmp/publish-', log)
        self.assertIn('-m evals.report', log)
        self.assertIn('-m evals.phoenix', log)
        self.assertLess(log.index('-m evals.baseline'), log.index('-m evals.report'))
        self.assertIn('--volume', log)

    def test_editing_launcher_during_measurement_does_not_change_running_script(self):
        result, log = self.run_launcher(mutate=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('-m evals.report', log)
        self.assertIn('-m evals.phoenix', log)

    def test_import_runs_in_an_isolated_container_and_mounts_its_journal(self):
        result, log = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('exec -i -w /tmp/fixture-code popot-evals-phoenix-1', log)
        self.assertIn('phoenix-import', log)
        self.assertIn('/eval-run', log)

    def test_each_variant_uses_its_own_code_directory_in_api(self):
        result, log = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(':/app/evals/', log)
        self.assertIn('/tmp/fixture-code', log)
        self.assertIn('POPOT_EVAL_SOURCE_ROOT=/app', log)

    def test_failed_baseline_retains_capture_and_stops_before_import(self):
        result, log = self.run_launcher(fail=True)
        self.assertEqual(result.returncode, 7)
        self.assertIn('cp popot-ax-local-api-1:/tmp/fixture-baseline', log)
        self.assertNotIn('-m evals.report', log)
        self.assertNotIn('-m evals.phoenix', log)

    def test_matrix_flags_reach_baseline_and_native_reference(self):
        result, log = self.run_launcher(extra=['--model', 'p/model', '--case', 'tool_recovery'], matrix=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('-m evals.runtime', log)
        self.assertIn('--monitor --model p/model --case tool_recovery', log)
        self.assertIn('-m evals.phoenix /eval-run --endpoint http://phoenix:6006 --baseline', log)


if __name__ == '__main__':
    unittest.main()
