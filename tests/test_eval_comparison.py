"""Observed tradeoffs must not be presented as unconditional improvements."""
from copy import deepcopy
import unittest


def experiment(*, score=1, tokens=100, elapsed=10, model='provider/model', repetitions=1):
    return {'config': {'primary_model': model, 'harness': 'popot-http', 'memory_mode': 'disabled',
                       'suite_sha256': 'suite-v1', 'judge_fingerprint': 'judge-v1',
                       'profiles': {'engineer': {'model_parameters': {'max_tokens': 1000}}}},
            'report': {'planned_runs': repetitions, 'finished_at': 'done', 'results': [
                {'case_id': 'case', 'repetition': n + 1, 'model_pin_verified': True,
                 'metrics': {'objective_score': score, 'total_tokens': tokens, 'latency_seconds': elapsed,
                             'judge_score': .8, 'self_score': .9}}
                for n in range(repetitions)]}}


class EvalComparisonTests(unittest.TestCase):
    def test_new_snapshots_and_legacy_reports_preserve_workflow_comparisons(self):
        from evals.compare import parameters
        workflow = {'total_seconds': 600}
        self.assertEqual(parameters({'evals': {'workflow': workflow}})['workflow'], workflow)
        self.assertEqual(parameters({'baseline': {'workflow': workflow}})['workflow'], workflow)

    def test_lower_tokens_and_higher_quality_have_positive_improvement(self):
        from evals.compare import compare
        result = compare(experiment(score=.5), experiment(tokens=80, model='provider/new'))
        by_name = result['metrics']
        self.assertEqual(by_name['total_tokens']['assessment'], 'better')
        self.assertEqual(by_name['total_tokens']['delta'], -20)
        self.assertEqual(by_name['total_tokens']['improvement_percent'], 20)
        self.assertEqual(by_name['objective_score']['assessment'], 'better')
        self.assertEqual(result['verdict'], 'better')
        self.assertEqual(result['parameter_changes']['model']['candidate'], 'provider/new')

    def test_quality_loss_with_fewer_tokens_is_a_tradeoff(self):
        from evals.compare import compare
        result = compare(experiment(), experiment(score=.5, tokens=50))
        self.assertEqual(result['metrics']['objective_score']['assessment'], 'worse')
        self.assertEqual(result['metrics']['total_tokens']['assessment'], 'better')
        self.assertEqual(result['verdict'], 'tradeoff')

    def test_unknown_usage_and_zero_baseline_never_get_invented_percentages(self):
        from evals.compare import compare
        result = compare(experiment(tokens=0), experiment(tokens=None))
        self.assertEqual(result['metrics']['total_tokens']['assessment'], 'unknown')
        self.assertIsNone(result['metrics']['total_tokens']['delta'])
        result = compare(experiment(tokens=0), experiment(tokens=1))
        self.assertEqual(result['metrics']['total_tokens']['assessment'], 'worse')
        self.assertIsNone(result['metrics']['total_tokens']['improvement_percent'])

    def test_changed_suite_missing_trials_and_failed_capture_are_not_comparable(self):
        from evals.compare import compare
        baseline = experiment(repetitions=2)
        for candidate in (experiment(), experiment(repetitions=2)):
            candidate = deepcopy(candidate)
            if len(candidate['report']['results']) == 2:
                candidate['report']['results'][1]['result'] = {'runner_error': {'type': 'TimeoutError'}}
            result = compare(baseline, candidate)
            self.assertFalse(result['comparable'])
            self.assertEqual(result['verdict'], 'insufficient_data')
        candidate = experiment(repetitions=2)
        candidate['config']['suite_sha256'] = 'different'
        self.assertFalse(compare(baseline, candidate)['comparable'])

    def test_judge_change_only_invalidates_judge_comparison(self):
        from evals.compare import compare
        candidate = experiment(tokens=80)
        candidate['config']['judge_fingerprint'] = 'different'
        result = compare(experiment(), candidate)
        self.assertTrue(result['comparable'])
        self.assertEqual(result['metrics']['judge_score']['assessment'], 'unknown')
        self.assertEqual(result['metrics']['total_tokens']['assessment'], 'better')

    def test_metrics_count_steps_retries_tools_and_errors_from_real_events(self):
        from evals.metrics import measure
        events = [{'event': 'model_request', 'attempt': 1},
                  {'event': 'model_request', 'attempt': 2},
                  {'event': 'tool_call', 'agent_session_id': 'a', 'call_id': 'x', 'name': 'bash'},
                  {'event': 'tool_call', 'agent_session_id': 'b', 'call_id': 'x', 'name': 'bash'},
                  {'event': 'tool_error', 'agent_session_id': 'b', 'call_id': 'x'},
                  {'event': 'final_result_invalid'}, {'event': 'turn_started'},
                  {'event': 'turn_started', 'turn': 1},
                  {'event': 'model_timeout'}, {'event': 'model_http_error'}]
        row = {'passed': True, 'functional_passed': True, 'false_completion': False,
               'elapsed_seconds': 2, 'tokens': None, 'usage_complete': False}
        metrics = measure(row, events)
        self.assertEqual(metrics['steps'], 1)
        self.assertEqual(metrics['model_retries'], 1)
        self.assertEqual(metrics['llm_requests'], 2)
        self.assertEqual(metrics['tool_calls'], 2)
        self.assertEqual(metrics['tool_errors'], 1)
        self.assertEqual(metrics['format_errors'], 1)
        self.assertEqual(metrics['request_errors'], 2)
        self.assertEqual(metrics['coordination_turns'], 1)
        self.assertIsNone(metrics['total_tokens'])

    def test_higher_self_score_does_not_establish_better_quality(self):
        from evals.compare import compare
        candidate = experiment()
        candidate['report']['results'][0]['metrics']['self_score'] = 1
        result = compare(experiment(), candidate)
        self.assertEqual(result['metrics']['self_score']['assessment'], 'changed')
        self.assertEqual(result['verdict'], 'same')

    def test_unknown_quality_cannot_establish_overall_improvement(self):
        from evals.compare import compare
        candidate = experiment(tokens=50)
        candidate['report']['results'][0]['metrics']['objective_score'] = None
        self.assertEqual(compare(experiment(), candidate)['verdict'], 'insufficient_data')


if __name__ == '__main__':
    unittest.main()
