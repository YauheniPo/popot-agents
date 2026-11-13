"""A named baseline supplies the model; shared eval settings have one owner."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def unified_settings():
    return {'baseline': 'flash-max', 'variants': [
        {'name': 'flash-max', 'model': 'provider/flash',
         'parameters': {'max_tokens': 4000, 'reasoning_effort': 'max'}},
        {'name': 'flash-low', 'model': 'provider/flash',
         'parameters': {'max_tokens': 4000, 'reasoning_effort': 'low'}}],
        'repetitions': 2,
        'workflow': {'total_seconds': 600, 'turn_seconds': 300, 'max_turns': 2,
                     'max_no_progress': 1, 'verification_reserve_seconds': 60,
                     'max_child_recoveries': 1, 'max_concurrent': 1},
        'request_timeout_seconds': 660, 'inspection_timeout_seconds': 30}


class EvalSettingsTests(unittest.TestCase):
    def test_managed_matrix_includes_separately_pinned_free_models_with_supported_limits(self):
        from evals.settings import load_config
        settings = load_config()
        variants = {v['name']: v for v in settings['variants']}
        self.assertEqual(variants['north-free']['model'], 'openrouter/cohere/north-mini-code:free')
        self.assertEqual(variants['nemotron-free']['model'], 'nvidia/nvidia/nemotron-3.5-lightning-30b-a3b')
        for name in ('north-free', 'nemotron-free'):
            self.assertEqual(variants[name]['parameters'], {'max_tokens': 64000})
        self.assertEqual(settings['baseline'], 'flash-max')
        self.assertEqual(settings['repetitions'], 2)
        self.assertEqual(set(variants), {'flash-max', 'flash-low', 'glm-max', 'north-free', 'nemotron-free'})

    def load(self, document):
        from evals.settings import load_config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(document))
            return load_config(path)

    def test_shared_settings_do_not_duplicate_the_baseline_model(self):
        settings = unified_settings()
        self.assertEqual(self.load({'x-evals': settings, 'services': {}}), settings)
        self.assertNotIn('model', settings)

    def test_managed_configuration_uses_only_the_unified_section(self):
        from evals.settings import load_config
        document = json.loads((ROOT / 'evals/config.json').read_text())
        self.assertNotIn('x-baseline', document)
        self.assertNotIn('x-experiments', document)
        settings = load_config()
        self.assertEqual(settings, document['x-evals'])
        self.assertTrue(any(v['name'] == settings['baseline'] for v in settings['variants']))

    def test_ambiguous_or_stale_sections_fail_with_migration_guidance(self):
        for document in ({'x-baseline': {}}, {'x-evals': unified_settings(), 'x-experiments': {}}):
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, 'x-evals'):
                self.load(document)

    def test_invalid_baseline_variants_parameters_and_budgets_are_rejected(self):
        changes = [
            ('baseline', 'missing'), ('baseline', None), ('variants', []), ('variants', {}),
            ('variants', [None]),
            ('variants', unified_settings()['variants'] * 2),
            ('variants', [{'name': 'flash-max', 'model': '', 'parameters': {}}]),
            ('variants', [{'name': '../escape', 'model': 'p/m', 'parameters': {}}]),
            ('variants', [{'name': 'flash-max', 'model': 'p/m', 'parameters': {'max_tokens': -1}}]),
            ('repetitions', True), ('repetitions', 0), ('repetitions', 4),
            ('request_timeout_seconds', 100), ('inspection_timeout_seconds', -1),
            ('model', 'duplicate/default'),
        ]
        for key, value in changes:
            settings = deepcopy(unified_settings())
            settings[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.load({'x-evals': settings})
        settings = unified_settings()
        settings['workflow']['turn_seconds'] = 700
        with self.assertRaises(ValueError):
            self.load({'x-evals': settings})
