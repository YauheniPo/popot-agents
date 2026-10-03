import json
import tempfile
import unittest
from pathlib import Path

from ax_local.config import AX_CONFIG, load_ax_config


class AxConfigTests(unittest.TestCase):
    def test_delegation_runtime_settings_do_not_require_role_permissions(self):
        settings = json.loads(json.dumps(AX_CONFIG))
        settings["delegation"].pop("allowed_roles", None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(settings))
            self.assertEqual(load_ax_config(path), settings)


if __name__ == "__main__":
    unittest.main()
