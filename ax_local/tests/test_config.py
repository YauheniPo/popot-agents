import json
import tempfile
import unittest
from pathlib import Path

from ax_local.config import AX_CONFIG, load_ax_config


class AxConfigTests(unittest.TestCase):
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
