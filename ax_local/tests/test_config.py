import json
import tempfile
import unittest
from pathlib import Path

from ax_local.config import AX_CONFIG, load_ax_config


class AxConfigTests(unittest.TestCase):
    def test_worker_pool_name_default_and_validation(self):
        settings = json.loads(json.dumps(AX_CONFIG))
        settings['ax'].pop('worker_pool_name', None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(settings))
            self.assertEqual(load_ax_config(path)['ax']['worker_pool_name'], 'popot-workers')
            for value in ('Uppercase', '-invalid', 'invalid-', 'a' * 64, '', None):
                settings['ax']['worker_pool_name'] = value
                path.write_text(json.dumps(settings))
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'worker_pool_name'):
                    load_ax_config(path)

    def test_legacy_child_capacity_is_ignored_in_favor_of_shared_worker_queue(self):
        self.assertNotIn('max_concurrent_tasks', AX_CONFIG['delegation'])
        settings = json.loads(json.dumps(AX_CONFIG))
        settings['delegation']['max_concurrent_tasks'] = 3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(settings))
            loaded = load_ax_config(path)
            self.assertNotIn('max_concurrent_tasks', loaded['delegation'])
            self.assertEqual(loaded['ax']['max_workers'], 10)
            self.assertEqual(loaded['ax']['worker_queue_timeout_seconds'], 3600)

    def test_worker_capacity_and_queue_timeout_validate_and_default_legacy_configs(self):
        for key in ('max_workers', 'worker_queue_timeout_seconds'):
            for value in (0, -1, True, '10', None):
                settings = json.loads(json.dumps(AX_CONFIG))
                settings['ax'][key] = value
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'config.json'
                    path.write_text(json.dumps(settings))
                    with self.assertRaisesRegex(ValueError, key):
                        load_ax_config(path)
        settings = json.loads(json.dumps(AX_CONFIG))
        settings['ax'].pop('max_workers')
        settings['ax'].pop('worker_queue_timeout_seconds')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(settings))
            loaded = load_ax_config(path)['ax']
            self.assertEqual(loaded['max_workers'], 10)
            self.assertEqual(loaded['worker_queue_timeout_seconds'], 3600)

    def test_suspend_retention_is_two_hours_and_requires_positive_seconds(self):
        self.assertEqual(AX_CONFIG['ax']['suspend_retention_seconds'], 7200)
        for value in (0, -1, True, '7200', None):
            settings = json.loads(json.dumps(AX_CONFIG))
            settings['ax']['suspend_retention_seconds'] = value
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'config.json'
                path.write_text(json.dumps(settings))
                with self.assertRaisesRegex(ValueError, 'suspend_retention_seconds'):
                    load_ax_config(path)
        settings = json.loads(json.dumps(AX_CONFIG))
        settings['ax'].pop('suspend_retention_seconds')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(settings))
            self.assertEqual(load_ax_config(path)['ax']['suspend_retention_seconds'], 7200)

    def test_proxy_response_margin_validation_and_legacy_default(self):
        for value in (0, -1, True, '5', None):
            settings = json.loads(json.dumps(AX_CONFIG))
            settings['proxy']['response_margin_seconds'] = value
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'config.json'
                path.write_text(json.dumps(settings))
                with self.assertRaisesRegex(ValueError, 'response_margin_seconds'):
                    load_ax_config(path)
        settings = json.loads(json.dumps(AX_CONFIG))
        settings['proxy'].pop('response_margin_seconds', None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(settings))
            self.assertEqual(load_ax_config(path)['proxy']['response_margin_seconds'], 5)

    def test_delegation_runtime_settings_do_not_require_role_permissions(self):
        settings = json.loads(json.dumps(AX_CONFIG))
        settings["delegation"].pop("allowed_roles", None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(settings))
            self.assertEqual(load_ax_config(path), settings)


if __name__ == "__main__":
    unittest.main()
