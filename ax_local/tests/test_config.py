import json
import tempfile
import unittest
from pathlib import Path

from popot_agents.runtime_config import RUNTIME
from ax_local.config import load_ax_config


class AxConfigTests(unittest.TestCase):
    def test_snapshot_mode_defaults_to_data_and_requires_explicit_supported_value(self):
        self.assertEqual(load_ax_config().get('snapshot_mode'), 'data')
        for value in ('auto', '', 'FULL', None, True, {}):
            settings = load_ax_config()
            settings['snapshot_mode'] = value
            with self.subTest(value=value):
                self.assert_invalid(settings, 'snapshot_mode')

    def assert_invalid(self, settings, field):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(settings))
            with self.assertRaisesRegex(ValueError, field):
                load_ax_config(path)

    def test_worker_pool_name_validation(self):
        for value in ('Uppercase', '-invalid', 'invalid-', 'a' * 64, '', None):
            settings = load_ax_config()
            settings['worker_pool_name'] = value
            with self.subTest(value=value):
                self.assert_invalid(settings, 'worker_pool_name')

    def test_worker_capacity_and_suspend_retention_require_positive_integers(self):
        for key in ('max_workers', 'worker_queue_timeout_seconds', 'suspend_retention_seconds'):
            for value in (0, -1, True, '10', None):
                settings = load_ax_config()
                settings[key] = value
                with self.subTest(key=key, value=value):
                    self.assert_invalid(settings, key)

    def test_proxy_response_margin_validation(self):
        for value in (0, -1, True, '5', None):
            settings = load_ax_config()
            settings['proxy']['response_margin_seconds'] = value
            with self.subTest(value=value):
                self.assert_invalid(settings, 'response_margin_seconds')

    def test_removed_or_missing_settings_are_not_silently_defaulted(self):
        for key in ('worker_pool_name', 'max_workers', 'worker_queue_timeout_seconds',
                    'suspend_retention_seconds'):
            settings = load_ax_config()
            del settings[key]
            with self.subTest(key=key):
                self.assert_invalid(settings, 'ax settings')
        settings = load_ax_config()
        settings['delegation']['max_concurrent_tasks'] = 3
        self.assert_invalid(settings, 'delegation settings')
        settings = load_ax_config()
        del settings['proxy']['response_margin_seconds']
        self.assert_invalid(settings, 'proxy settings')

    def test_delegation_ports_and_budgets_are_validated(self):
        settings = load_ax_config()
        settings['delegation']['port'] = settings['proxy']['port']
        self.assert_invalid(settings, 'ports must differ')
        settings = load_ax_config()
        settings['delegation']['turn_timeout_seconds'] = RUNTIME['timeouts']['max_turn_seconds'] + 1
        self.assert_invalid(settings, 'turn_timeout_seconds')
        for value in (True, 0, float('nan'), float('inf')):
            settings = load_ax_config()
            settings['delegation']['poll_interval_seconds'] = value
            with self.subTest(value=value):
                self.assert_invalid(settings, 'poll_interval_seconds')

    def test_ax_shape_and_local_context_are_validated(self):
        for value in (None, [], {'context': 'kind-test'}):
            settings = value
            with self.subTest(value=value):
                self.assert_invalid(settings, 'ax settings')
        settings = load_ax_config()
        settings['context'] = 'production'
        self.assert_invalid(settings, 'local kind')


if __name__ == '__main__':
    unittest.main()
