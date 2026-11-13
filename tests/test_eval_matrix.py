"""A matrix preserves exact parameters and compares only against its selected baseline."""
from pathlib import Path
import json
import fcntl
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from evals import save
from tests.test_eval_comparison import experiment
from tests.test_eval_settings import unified_settings


class EvalMatrixTests(unittest.TestCase):
    def test_concurrent_resume_is_rejected_before_launch(self):
        from evals.matrix import run
        config = unified_settings()
        config['variants'] = [v for v in config['variants'] if v['name'] == config['baseline']]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            save(output / 'matrix.json', {'configuration': config, 'cases': [], 'run_id': 'saved',
                                          'runs': {}, 'comparisons': {}})
            with (output / '.matrix.lock').open('a') as lock, \
                    patch('evals.matrix.subprocess.run') as launch:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    run(output, config, resume=True)
                launch.assert_not_called()

    def test_failed_comparison_is_retried_without_repeating_completed_variants(self):
        from evals.matrix import run
        config = {**unified_settings(), 'baseline': 'a', 'variants': [
            {'name': name, 'model': 'provider/' + name, 'parameters': {}} for name in ('a', 'b')]}

        def launch(command, **kwargs):
            directory = Path(kwargs['env']['POPOT_EVAL_OUTPUT']) / 'baseline'
            for key, value in experiment().items():
                save(directory / (key + '.json'), value)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'matrix'
            with patch('evals.matrix.subprocess.run', side_effect=launch), \
                    patch('evals.metrics.publish_comparison', return_value={'ok': False}):
                with self.assertRaisesRegex(RuntimeError, 'comparison'):
                    run(output, config)
            self.assertEqual(json.loads((output / 'matrix.json').read_text())['status'], 'failed')
            with patch('evals.matrix.subprocess.run') as launcher, \
                    patch('evals.metrics.publish_comparison', return_value={'ok': True}):
                result = run(output, config, resume=True)
                launcher.assert_not_called()
            self.assertEqual(result['status'], 'completed')
            self.assertIn('b', result['comparisons'])

    def test_publication_failure_is_recorded_and_resume_does_not_repeat_agents(self):
        from evals.matrix import run
        config = {**unified_settings(), 'baseline': 'a', 'variants': [
            {'name': 'a', 'model': 'provider/a', 'parameters': {}}]}
        commands = []

        def fail_publication(command, **kwargs):
            commands.append(command)
            directory = Path(kwargs['env']['POPOT_EVAL_OUTPUT']) / 'baseline'
            save(directory / 'report.json', {'finished_at': 'now', 'planned_runs': 1, 'results': [{}]})
            raise subprocess.CalledProcessError(137, command)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'matrix'
            with patch('evals.matrix.subprocess.run', side_effect=fail_publication):
                with self.assertRaises(subprocess.CalledProcessError):
                    run(output, config)
            failed = json.loads((output / 'matrix.json').read_text())
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(failed['failed_variant'], 'a')
            self.assertEqual(failed['exit_code'], 137)
            with patch('evals.matrix.subprocess.run') as launch:
                result = run(output, config, resume=True)
                command = launch.call_args.args[0]
                self.assertTrue(command[1].endswith('/evals/publish.sh'))
                self.assertIn('--baseline', command)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['run_id'], failed['run_id'])
            self.assertNotIn('failed_variant', result)

    def test_resume_rejects_config_changes_and_incomplete_capture(self):
        from evals.matrix import run
        config = {**unified_settings(), 'baseline': 'a', 'variants': [
            {'name': 'a', 'model': 'provider/a', 'parameters': {}}]}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'matrix'
            save(output / 'matrix.json', {'configuration': config, 'cases': [], 'run_id': 'saved', 'runs': {}})
            save(output / 'a/baseline/report.json', {'planned_runs': 2, 'results': [{}]})
            with patch('evals.matrix.subprocess.run') as launch:
                with self.assertRaisesRegex(ValueError, 'incomplete'):
                    run(output, config, resume=True)
                with self.assertRaisesRegex(ValueError, 'configuration'):
                    run(output, {**config, 'repetitions': 3}, resume=True)
                launch.assert_not_called()

    def test_dataset_label_uses_native_creation_datetime_in_utc(self):
        from evals.names import dataset_label
        api = Mock()
        api.query.return_value = {'node': {'name': 'popot-harness-local-782a0561684e',
                                          'createdAt': '2026-11-13T00:39:00+01:00'}}
        self.assertEqual(dataset_label(api, 'RGF0YXNldDo2'),
                         'popot-harness-local-782a0561684e · 2026-11-12 23:39:00 UTC')
        self.assertEqual(api.query.call_args.args[1], {'id': 'RGF0YXNldDo2'})

    def test_phoenix_number_is_the_shared_visual_name_and_is_read_back(self):
        from evals.names import rename
        config = {'primary_model': 'ollama-cloud/deepseek-v4.1-flash:1017',
                  'run_id': '20261113T143602-9a12d9',
                  'profiles': {'agent': {'model_parameters': {'reasoning_effort': 'max'}}},
                  'evaluation': {'version': 'popot-evals-v2'}}
        api = Mock()
        expected = 'deepseek-v4.1-flash:1017-max-judge-v2-20261113T143602-9a12d9-11'
        api.query.side_effect = [{}, {'node': {'name': expected}}]
        self.assertEqual(rename(api, config, 'RXhwZXJpbWVudDoxMQ=='), expected)
        self.assertEqual(api.query.call_args_list[0].args[1]['input'],
                         {'experimentId': 'RXhwZXJpbWVudDoxMQ==', 'name': expected})
        api.query.side_effect = [{}, {'node': {'name': 'not-updated'}}]
        with self.assertRaises(RuntimeError):
            rename(api, config, 'RXhwZXJpbWVudDoxMQ==')

    def test_legacy_matrix_label_keeps_original_run_id_in_display_name(self):
        from evals.names import rename
        config = {'primary_model': 'ollama-cloud/glm-5.3-flash',
                  'profiles': {'agent': {'model_parameters': {'reasoning_effort': 'max'}}},
                  'experiment_name': 'matrix-20261112T211439-e7e39f-glm-max'}
        expected = 'glm-5.3-flash-max-evals-20261112T211439-e7e39f-11'
        api = Mock()
        api.query.side_effect = [{}, {'node': {'name': expected}}]
        self.assertEqual(rename(api, config, 'RXhwZXJpbWVudDoxMQ=='), expected)

    def test_experiment_names_start_with_exact_model_effort_and_measurement_kind(self):
        from evals.names import experiment_name
        self.assertEqual(experiment_name('ollama-cloud/deepseek-v4.1-flash:1017',
                                        {'reasoning_effort': 'max'}, 'evals', '20261112T211439-e7e39f'),
                         'deepseek-v4.1-flash:1017-max-evals-20261112T211439-e7e39f')
        self.assertEqual(experiment_name('ollama-cloud/glm-5.3-flash',
                                        {'reasoning_effort': 'max'}, 'judge-v2', '20261112T211439-e7e39f'),
                         'glm-5.3-flash-max-judge-v2-20261112T211439-e7e39f')

    def test_matrix_forwards_variants_and_exports_paired_comparison(self):
        from evals.matrix import run
        config = {**unified_settings(), 'baseline': 'a', 'repetitions': 3, 'variants': [
            {'name': 'a', 'model': 'provider/a', 'parameters': {'max_tokens': 100}},
            {'name': 'b', 'model': 'provider/b', 'parameters': {'max_tokens': 200}}]}
        commands = []

        def launch(command, **kwargs):
            commands.append(command)
            directory = Path(kwargs['env']['POPOT_EVAL_OUTPUT']) / 'baseline'
            data = experiment(tokens=100 if command[2].endswith('-a') else 80)
            data['config']['experiment_name'] = command[2]
            for key, value in data.items():
                save(directory / (key + '.json'), value)

        with tempfile.TemporaryDirectory() as tmp, patch('evals.matrix.subprocess.run', side_effect=launch), \
                patch('evals.metrics.publish_comparison', return_value={'ok': True}):
            output = Path(tmp) / 'matrix'
            result = run(output, config, cases=['tool_recovery'])
            self.assertTrue((output / 'b/baseline/comparison.md').exists())
        self.assertEqual(len(commands), 2)
        self.assertTrue(commands[0][commands[0].index('--experiment') + 1].startswith('a-default-evals-'))
        self.assertTrue(commands[1][commands[1].index('--experiment') + 1].startswith('b-default-evals-'))
        self.assertIn('--parameters', commands[1])
        self.assertIn('--case', commands[1])
        self.assertEqual(commands[1][commands[1].index('--repetitions') + 1], '3')
        self.assertEqual(result['comparisons']['b']['verdict'], 'better')

    def test_bad_baseline_or_duplicate_names_fail_before_launch(self):
        from evals.matrix import run
        for config in ({'baseline': 'missing', 'variants': [{'name': 'a', 'model': 'p/a', 'parameters': {}}]},
                       {'baseline': 'a', 'variants': [{'name': 'a', 'model': 'p/a', 'parameters': {}}] * 2}):
            with tempfile.TemporaryDirectory() as tmp, patch('evals.matrix.subprocess.run') as launch:
                with self.assertRaises(ValueError):
                    run(Path(tmp) / 'matrix', {**unified_settings(), **config})
                launch.assert_not_called()

    def test_cli_baseline_selection_and_repetition_override_share_one_configuration(self):
        from evals.matrix import main
        config = unified_settings()
        with patch('evals.matrix.load_config', return_value=config), \
                patch('evals.matrix.run') as launch, \
                patch('sys.argv', ['matrix', '--output', '/tmp/matrix-test', '--variant', 'flash-low',
                                   '--baseline', 'flash-low', '--repetitions', '3']):
            main()
        self.assertEqual(launch.call_args.args[1]['baseline'], 'flash-low')
        self.assertEqual([v['name'] for v in launch.call_args.args[1]['variants']], ['flash-low'])
        self.assertEqual(launch.call_args.kwargs['repetitions'], 3)
        self.assertFalse(launch.call_args.kwargs['resume'])
