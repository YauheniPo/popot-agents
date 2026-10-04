import unittest
from unittest.mock import patch

from ax_local.worker.entrypoint import main


class EntrypointTests(unittest.TestCase):
    def test_workspace_setup_does_not_require_chown_capability(self):
        with patch("ax_local.worker.entrypoint.os.makedirs"), \
             patch("ax_local.worker.entrypoint.os.geteuid", return_value=0), \
             patch("ax_local.worker.entrypoint.os.chown",
                   side_effect=PermissionError("Operation not permitted")), \
             patch("ax_local.worker.entrypoint.os.chmod") as chmod, \
             patch("ax_local.worker.entrypoint.serve") as serve:
            main()
        chmod.assert_called_once_with("/workspace", 0o1777)
        serve.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
