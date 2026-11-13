"""The manual launcher runs both eval suites and retains a failed capture."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RunAllEvalTests(unittest.TestCase):
    def run_launcher(self, fail=False, extra=(), matrix=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'evals').mkdir()
            shutil.copyfile(ROOT / 'evals/run-all.sh', root / 'evals/run-all.sh')
            shutil.copyfile(ROOT / 'evals/config.json', root / 'evals/config.json')
            tools = root / 'bin'
            tools.mkdir()
            docker = tools / 'docker'
            docker.write_text('#!/usr/bin/env bash\n'
                              'printf "%s\\n" "$*" >> "$EVAL_TEST_LOG"\n'
                              'if [[ "$1" == cp && "$2" == *:* && "$3" != *:* && "$3" != *.json ]]; then mkdir -p "$3"; fi\n'
                              'if [[ "$*" == *"-m evals.baseline"* && "${EVAL_TEST_FAIL:-}" == 1 ]]; then exit 7; fi\n'
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
                   'EVAL_TEST_LOG': str(log), 'EVAL_TEST_FAIL': '1' if fail else ''}
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
        self.assertIn('node /tmp/fixture-gateway.mjs /tmp/fixture-report.json', log)
        self.assertIn('-m evals.report', log)
        self.assertIn('-m evals.phoenix', log)
        self.assertIn('/popot_agents/telemetry.py', log)
        self.assertIn('/popot_agents/progress.py', log)
        self.assertLess(log.index('-m evals.baseline'), log.index('-m evals.report'))
        self.assertIn('phoenix.json', log)

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
        self.assertIn('-m evals.phoenix /tmp/fixture-baseline --baseline', log)


if __name__ == '__main__':
    unittest.main()
