import unittest


class EvalDashboardTests(unittest.TestCase):
    def test_renamed_experiments_select_latest_alias_before_joining_panels(self):
        from ax_local.cluster.eval_dashboard import dashboard
        variable = next(v for v in dashboard()['templating']['list'] if v['name'] == 'experiment')
        self.assertIn('display_name_updated_at_seconds', variable['query'])
        self.assertIn('topk by(series,experiment)', variable['query'])
        self.assertIn('clamp_max(', variable['query'])

    def test_experiment_picker_includes_progress_before_phoenix_publication(self):
        from ax_local.cluster.eval_dashboard import dashboard
        variable = next(v for v in dashboard()['templating']['list'] if v['name'] == 'experiment')
        self.assertIn('popot_eval_progress', variable['query'])
        self.assertIn('on(series,experiment)', variable['query'])

    def test_metrics_are_categorized_with_raw_and_delta_side_by_side(self):
        from ax_local.cluster.eval_dashboard import dashboard
        from evals.metrics import METRICS
        expected = {
            'Обзор запуска': [],
            'Качество результата': ['objective_score', 'functional_success', 'false_completion'],
            'Оценки judge и агента': ['judge_score', 'self_score'],
            'Токены': ['total_tokens', 'prompt_tokens', 'completion_tokens', 'judge_tokens'],
            'Время выполнения': ['latency_seconds'],
            'Действия harness': ['steps', 'llm_requests', 'tool_calls', 'coordination_turns'],
            'Ошибки и повторы': ['request_errors', 'tool_errors', 'format_errors', 'model_retries'],
            'Параметры и ссылки': [],
        }
        panels = dashboard()['panels']
        rows = [p for p in panels if p['type'] == 'row']
        self.assertEqual([p['title'] for p in rows], list(expected))
        self.assertEqual(len({p['id'] for p in panels}), len(panels))
        self.assertTrue(all(p['collapsed'] is False for p in rows))
        seen, category = set(), None
        for panel in panels:
            if panel['type'] == 'row':
                category = panel['title']
            if 'popot_eval_value{' not in str(panel):
                continue
            name = next(n for n in METRICS if f'metric="{n}"' in str(panel))
            self.assertIn(name, expected[category])
            self.assertNotIn(name, seen)
            seen.add(name)
            if METRICS[name][0] != 'NONE':
                delta = next(p for p in panels if 'popot_eval_improvement{' in str(p)
                             and f'metric="{name}"' in str(p))
                self.assertEqual(panel['gridPos']['y'], delta['gridPos']['y'])
                self.assertEqual(panel['gridPos']['x'], 0)
                self.assertEqual(delta['gridPos']['x'], 12)
                self.assertEqual(panel['gridPos']['w'], 12)
                self.assertEqual(delta['gridPos']['w'], 12)
        self.assertEqual(seen, set(METRICS))

    def test_suite_choice_displays_dataset_name_and_date_without_changing_the_series_key(self):
        from ax_local.cluster.eval_dashboard import dashboard
        series = next(v for v in dashboard()['templating']['list'] if v['name'] == 'series')
        self.assertIn('series_display_name', series['query'])
        self.assertIn('popot_eval_info', series['query'])
        self.assertIn('popot_eval_progress', series['query'])
        self.assertIn('(?<text>', series['regex'])
        self.assertIn('(?<value>', series['regex'])

    def test_info_publisher_keeps_native_dataset_label_with_historical_metric_identity(self):
        from unittest.mock import patch
        from evals.metrics import publish_info
        label = 'popot-harness-local-782a0561684e · 2026-11-12 23:39:00 UTC'
        with patch('evals.metrics.emit', return_value={'ok': True}) as emit:
            publish_info({'suite_sha256': '782a0561684e6c21', 'experiment_name': 'stored-key',
                          'evals': {'parallel_models': 3}},
                         phoenix_url='http://localhost/compare', display_name='model-max-evals-16',
                         series_display_name=label)
        self.assertGreater(emit.call_args.args[1]['display_name_updated_at_seconds'], 0)
        self.assertEqual(emit.call_args.args[1]['configuration'], 1)
        labels = emit.call_args.args[0]
        self.assertEqual(labels['parallel_models'], '3')
        self.assertEqual(labels['series_display_name'], label)
        self.assertEqual(labels['series'], '782a0561684e6c21')
        self.assertEqual(labels['experiment'], 'stored-key')

    def test_dashboard_has_explicit_baseline_and_direction_aware_comparison(self):
        from ax_local.cluster.eval_dashboard import dashboard
        result = dashboard()
        variables = {v['name'] for v in result['templating']['list']}
        self.assertLessEqual({'series', 'experiment', 'baseline'}, variables)
        serialized = str(result)
        for text in ('popot_eval', 'popot-prometheus', 'baseline', 'objective_score', 'judge_score', 'tool_calls'):
            self.assertIn(text, serialized)
        deltas = [p for p in result['panels'] if 'Δ' in p['title']]
        self.assertTrue(deltas)
        for panel in deltas:
            self.assertIn('больше — лучше', panel['title'])
            self.assertNotIn('or vector(0)', str(panel))
        raw = next(p for p in result['panels'] if p['title'] == 'Токены всего: меньше — лучше ↓')
        self.assertEqual(raw['fieldConfig']['defaults']['thresholds']['steps'], [{'color': 'blue', 'value': None}])

    def test_friendly_names_keep_original_metric_keys_for_historical_queries(self):
        from ax_local.cluster.eval_dashboard import dashboard
        result = dashboard()
        variables = {v['name']: v for v in result['templating']['list']}
        for name in ('experiment', 'baseline'):
            self.assertIn('(?<text>', variables[name]['regex'])
            self.assertIn('(?<value>', variables[name]['regex'])
            self.assertIn('popot_eval_info', variables[name]['query'])
        for panel in result['panels']:
            if panel['type'] == 'stat':
                self.assertEqual(panel['targets'][0]['legendFormat'], '{{display_name}}')
                self.assertIn('group_left(display_name)', panel['targets'][0]['expr'])

    def test_every_metric_explains_raw_direction_and_normalized_delta(self):
        from ax_local.cluster.eval_dashboard import dashboard
        from evals.metrics import METRICS
        panels = dashboard()['panels']
        for name, (direction, label, _) in METRICS.items():
            with self.subTest(metric=name):
                raw = next(p for p in panels if 'popot_eval_value{' in str(p) and
                           f'metric="{name}"' in str(p))
                if direction == 'NONE':
                    self.assertIn('самоотчёт', raw['title'])
                    continue
                self.assertIn('меньше — лучше' if direction == 'MINIMIZE' else 'больше — лучше', raw['title'])
                delta = next(p for p in panels if 'popot_eval_improvement{' in str(p) and
                             f'metric="{name}"' in str(p))
                self.assertIn('Δ улучшения', delta['title'])
                self.assertIn('baseline − эксперимент' if direction == 'MINIMIZE' else
                              'эксперимент − baseline', delta['description'])
                if name == 'total_tokens':
                    self.assertIn('1000', delta['description'])

    def test_otlp_payload_omits_unknowns_and_carries_no_task_content(self):
        from evals.metrics import payload
        body = payload({'experiment': 'run-a', 'model': 'model', 'series': 'suite'},
                       {'total_tokens': None, 'objective_score': 1})
        points = body['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0]['gauge']['dataPoints']
        self.assertEqual(len(points), 1)
        attrs = {a['key']: a['value']['stringValue'] for a in points[0]['attributes']}
        self.assertEqual(attrs['metric'], 'objective_score')
        self.assertEqual(points[0]['asDouble'], 1)


if __name__ == '__main__':
    unittest.main()
