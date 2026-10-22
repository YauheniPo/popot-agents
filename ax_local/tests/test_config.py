import json
import tempfile
import unittest
from pathlib import Path

from popot_agents.runtime_config import load_runtime_config


class AxConfigTests(unittest.TestCase):
    def assert_invalid(self, settings, field):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime.json'
            path.write_text(json.dumps(settings))
            with self.assertRaisesRegex(ValueError, field):
                load_runtime_config(path)

    def test_worker_pool_name_validation(self):
        for value in ('Uppercase', '-invalid', 'invalid-', 'a' * 64, '', None):
            settings = load_runtime_config()
            settings['ax']['worker_pool_name'] = value
            with self.subTest(value=value):
                self.assert_invalid(settings, 'worker_pool_name')

    def test_worker_capacity_and_suspend_retention_require_positive_integers(self):
        for key in ('max_workers', 'worker_queue_timeout_seconds', 'suspend_retention_seconds'):
            for value in (0, -1, True, '10', None):
                settings = load_runtime_config()
                settings['ax'][key] = value
                with self.subTest(key=key, value=value):
                    self.assert_invalid(settings, key)

    def test_proxy_response_margin_validation(self):
        for value in (0, -1, True, '5', None):
            settings = load_runtime_config()
            settings['ax']['proxy']['response_margin_seconds'] = value
            with self.subTest(value=value):
                self.assert_invalid(settings, 'response_margin_seconds')

    def test_removed_or_missing_settings_are_not_silently_defaulted(self):
        for key in ('worker_pool_name', 'max_workers', 'worker_queue_timeout_seconds',
                    'suspend_retention_seconds'):
            settings = load_runtime_config()
            del settings['ax'][key]
            with self.subTest(key=key):
                self.assert_invalid(settings, 'ax settings')
        settings = load_runtime_config()
        settings['ax']['delegation']['max_concurrent_tasks'] = 3
        self.assert_invalid(settings, 'delegation settings')
        settings = load_runtime_config()
        del settings['ax']['proxy']['response_margin_seconds']
        self.assert_invalid(settings, 'proxy settings')

    def test_delegation_ports_and_budgets_are_validated(self):
        settings = load_runtime_config()
        settings['ax']['delegation']['port'] = settings['ax']['proxy']['port']
        self.assert_invalid(settings, 'ports must differ')
        settings = load_runtime_config()
        settings['ax']['delegation']['turn_timeout_seconds'] = settings['timeouts']['max_turn_seconds'] + 1
        self.assert_invalid(settings, 'turn_timeout_seconds')
        for value in (True, 0, float('nan'), float('inf')):
            settings = load_runtime_config()
            settings['ax']['delegation']['poll_interval_seconds'] = value
            with self.subTest(value=value):
                self.assert_invalid(settings, 'poll_interval_seconds')

    def test_ax_shape_and_local_context_are_validated(self):
        for value in (None, [], {'context': 'kind-test'}):
            settings = load_runtime_config()
            settings['ax'] = value
            with self.subTest(value=value):
                self.assert_invalid(settings, 'ax settings')
        settings = load_runtime_config()
        settings['ax']['context'] = 'production'
        self.assert_invalid(settings, 'local kind')


if __name__ == '__main__':
    unittest.main()
