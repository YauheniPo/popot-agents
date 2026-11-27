"""The operator runs the versioned evaluator using its own read-only credential."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from popot_agents.artifacts import collect_artifacts


ROOT = Path(__file__).resolve().parents[1]


def observation():
    return {'context': {'span_id': '1' * 16, 'trace_id': '2' * 32},
            'name': 'model', 'span_kind': 'LLM', 'parent_id': None, 'status_code': 'OK',
            'start_time': '2026-11-12T10:00:00+00:00', 'end_time': '2026-11-12T10:00:02+00:00',
            'attributes': {'session.id': 'root', 'popot.root_session_id': 'root',
                           'popot.agent_session_id': 'root', 'popot.role': 'planner',
                           'llm.model_name': 'actual', 'llm.token_count.total': 10,
                           'input.value': 'private task'}, 'events': []}


class OperatorEvalTests(unittest.TestCase):
    def test_runner_reads_phoenix_with_role_key_and_exports_real_report_artifacts(self):
        from evals.operator_tools import RuntimeEvals
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('os.environ', {'PHOENIX_API_KEY': 'viewer-value',
                                          'PHOENIX_ADMIN_SECRET': 'admin-value'}), \
                patch('evals.operator_tools.BoundedAPI') as api_type:
            api_type.return_value.call.return_value = {'data': [observation()], 'next_cursor': None}
            runner = RuntimeEvals(tmp)
            result = runner.run(hours=12, end='2026-11-12T11:00:00Z', publish=False)
            self.assertEqual(result['summary']['spans'], 1)
            self.assertEqual(result['summary']['tokens'], 10)
            self.assertFalse(result['published'])
            self.assertEqual(api_type.call_args.args[1], 'viewer-value')
            api = api_type.return_value
            self.assertEqual(api.call.call_count, 1)
            self.assertNotIn('payload', api.call.call_args.kwargs)
            report = runner.read(result['run_id'], 'summary')
            self.assertEqual(report['llm_calls'], 1)
            artifacts = collect_artifacts(tmp)
            self.assertEqual({a['name'] for a in artifacts},
                             {'runtime-evals-report.json.gz', 'runtime-evals-summary.md'})
            report_bytes = (Path(tmp) / 'artifacts/runtime-evals-report.json.gz').read_bytes()
            exported = json.loads(gzip.decompress(report_bytes))
            self.assertEqual(exported['summary'], result['summary'])
            self.assertNotIn('private task', json.dumps(exported))
            self.assertNotIn('viewer-value', json.dumps(exported))
            self.assertNotIn('admin-value', json.dumps(exported))

    def test_publish_uses_the_same_role_key_and_returns_a_write_receipt(self):
        from evals.operator_tools import RuntimeEvals
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('os.environ', {'PHOENIX_API_KEY': 'member-value'}), \
                patch('evals.operator_tools.BoundedAPI') as api, \
                patch('evals.operator_tools.annotate', return_value=9) as annotate:
            api.return_value.call.return_value = {'data': [observation()], 'next_cursor': None}
            result = RuntimeEvals(tmp).run()
            self.assertTrue(result['published'])
            self.assertEqual(result['annotations'], 9)
            self.assertEqual(api.call_args.args[1], 'member-value')
            self.assertIs(annotate.call_args.args[0], api.return_value)

    def test_missing_role_key_never_falls_back_to_admin(self):
        from evals.operator_tools import RuntimeEvals
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('os.environ', {'PHOENIX_ADMIN_SECRET': 'admin-only'}, clear=True), \
                patch('evals.operator_tools.BoundedAPI') as api:
            with self.assertRaisesRegex(ValueError, 'PHOENIX_API_KEY'):
                RuntimeEvals(tmp).run()
            api.assert_not_called()

    def test_failed_publish_returns_saved_run_and_unknown_write_count(self):
        from evals.operator_tools import RuntimeEvals
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('os.environ', {'PHOENIX_API_KEY': 'member-value'}), \
                patch('evals.operator_tools.BoundedAPI') as api, \
                patch('evals.operator_tools.annotate', side_effect=RuntimeError('Phoenix HTTP 503')):
            api.return_value.call.return_value = {'data': [observation()], 'next_cursor': None}
            runner = RuntimeEvals(tmp)
            result = runner.run()
            self.assertFalse(result['published'])
            self.assertEqual(result['status'], 'failed_or_unknown')
            self.assertIsNone(result['annotations'])
            self.assertEqual(runner.read(result['run_id'], 'summary')['spans'], 1)

    def test_repeated_fixed_window_reuses_the_confirmed_run(self):
        from evals.operator_tools import RuntimeEvals
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('os.environ', {'PHOENIX_API_KEY': 'member-value'}), \
                patch('evals.operator_tools.BoundedAPI') as api, \
                patch('evals.operator_tools.annotate', return_value=9) as annotate:
            api.return_value.call.return_value = {'data': [observation()], 'next_cursor': None}
            runner = RuntimeEvals(tmp)
            first = runner.run(end='2026-11-12T11:00:00Z')
            second = runner.run(end='2026-11-12T11:00:00Z')
            self.assertEqual(first['run_id'], second['run_id'])
            self.assertTrue(second['reused'])
            self.assertEqual(annotate.call_count, 1)
            (runner.directory(first['run_id']) / 'publication.json').unlink()
            recovered = runner.run(end='2026-11-12T11:00:00Z')
            self.assertTrue(recovered['published'])
            self.assertTrue(recovered['reused'])
            self.assertEqual(annotate.call_count, 2)
            self.assertEqual(api.return_value.call.call_count, 1)

    def test_bad_windows_and_report_paths_fail_before_access(self):
        from evals.operator_tools import RuntimeEvals
        with tempfile.TemporaryDirectory() as tmp, patch('evals.operator_tools.BoundedAPI') as api:
            runner = RuntimeEvals(tmp)
            for hours in (True, 0, -1, 169, float('nan'), float('inf')):
                with self.subTest(hours=hours), self.assertRaises(ValueError):
                    runner.run(hours=hours)
            for run_id in ('../.env', '/etc/passwd', 'not-a-run'):
                with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                    runner.read(run_id, 'summary')
            with self.assertRaises(ValueError):
                runner.read('a' * 32, '../snapshot.json')
            api.assert_not_called()

    def test_capture_budget_stops_without_returning_a_partial_success(self):
        from evals.operator_tools import BoundedAPI
        api = BoundedAPI('http://localhost:6006', 'viewer', max_spans=1)
        with patch('evals.runtime.API.call', return_value={'data': [observation()], 'next_cursor': 'next'}):
            api.call('/spans')
            with self.assertRaisesRegex(ValueError, 'limit'):
                api.call('/spans')
        with patch('evals.operator_tools.time.monotonic', return_value=api.deadline + 1), \
                patch('evals.runtime.API.call') as underlying:
            with self.assertRaisesRegex(TimeoutError, 'deadline'):
                api.call('/spans')
            underlying.assert_not_called()

    def test_only_operator_loads_eval_tools_and_runbook_is_in_effective_prompt(self):
        from popot_agents.skills import prepare_role_config
        roles = {p.stem: json.loads(p.read_text()) for p in (ROOT / 'config/roles').glob('*.json')}
        operator = roles['project_operator']
        self.assertEqual(operator['mcpServers']['evals'], {
            'command': ['python', '/app/evals/operator_tools.py'],
            'tools': ['run_runtime_evals', 'read_eval_report']})
        self.assertEqual({name for name, role in roles.items() if 'evals' in role.get('mcpServers', {})},
                         {'project_operator'})
        self.assertNotIn('PHOENIX_ADMIN_SECRET', operator['env_names'])
        self.assertNotIn('bash', operator['tools'])
        prepared = prepare_role_config(operator)
        for text in ('run_runtime_evals', 'read_eval_report', 'functional_success',
                     'cases.json', 'VIEWER', '64', 'PHOENIX_API_KEY'):
            self.assertIn(text, prepared['instructions'])

    def test_operator_can_locate_dashboards_and_query_comparable_metric_evidence(self):
        from popot_agents.skills import prepare_role_config
        operator = json.loads((ROOT / 'config/roles/project_operator.json').read_text())
        instructions = prepare_role_config(operator)['instructions']
        for text in ('http://127.0.0.1:3002/d/popot-evals', 'kind=metrics',
                     'popot_eval_info', 'popot_eval_progress', 'popot_eval_comparison',
                     'popot_eval_value', 'popot_eval_improvement', 'last_over_time',
                     'series', 'baseline', 'baseline − experiment', 'experiment − baseline',
                     'judge_fingerprint', 'self_score', 'display_name', 'Phoenix experiment number'):
            with self.subTest(text=text):
                self.assertIn(text, instructions)

    def test_operator_guidance_covers_current_comparison_limits(self):
        from popot_agents.skills import prepare_role_config
        operator = json.loads((ROOT / 'config/roles/project_operator.json').read_text())
        instructions = prepare_role_config(operator)['instructions']
        for tool in operator['mcpServers']['evals']['tools']:
            self.assertIn('mcp__evals__' + tool, instructions)
        for text in ('compare different dates', 'same cases and repetitions',
                     'different matrix runs', 'judge_score', 'other independently measured metrics',
                     'MCP discovery', 'independent judge', 'telemetry_verification',
                     'Phoenix experiments and saved reports', 'separate retention',
                     'Качество результата', 'Ошибки и повторы'):
            with self.subTest(text=text):
                self.assertTrue(text in instructions, 'Runbook must cover: ' + text)

    def test_worker_packages_evaluator_and_local_mcp_entrypoint(self):
        dockerfile = (ROOT / 'ax_local/docker/worker.Dockerfile').read_text()
        ignored = (ROOT / 'ax_local/docker/worker.Dockerfile.dockerignore').read_text()
        for path in ('evals/__init__.py', 'evals/runtime.py', 'evals/operator_tools.py', 'evals/config.json'):
            self.assertIn(path, dockerfile)
            self.assertIn('!' + path, ignored)

    def test_deployment_manages_only_the_configured_operator_membership(self):
        from evals.phoenix_providers import provision_operator
        api = Mock()
        viewer = {'id': 'operator', 'email': 'operator@example.com', 'role': {'name': 'VIEWER'}}
        member = {**viewer, 'role': {'name': 'MEMBER'}}
        page = lambda user: {'users': {'edges': [{'node': user}],
                                     'pageInfo': {'hasNextPage': False, 'endCursor': None}}}
        api.query.side_effect = [page(viewer), {'patchUser': {'user': member}}, page(member)]
        result = provision_operator({'email': viewer['email'], 'role': 'MEMBER'}, api)
        self.assertEqual(result['role'], 'MEMBER')
        mutation = api.query.call_args_list[1]
        self.assertEqual(mutation.args[1], {'input': {'userId': 'operator', 'newRole': 'MEMBER'}})
        api.reset_mock()
        api.query.side_effect = [page(member)]
        self.assertEqual(provision_operator({'email': viewer['email'], 'role': 'MEMBER'}, api)['role'], 'MEMBER')
        self.assertEqual(api.query.call_count, 1)


if __name__ == '__main__':
    unittest.main()
