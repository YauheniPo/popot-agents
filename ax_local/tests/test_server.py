import os
import unittest
from unittest.mock import patch

from ax_local.api.server import kind_control_plane_host, make_server, selected_provider_model


class AxServerTests(unittest.TestCase):
    def test_control_plane_host_follows_ax_context(self):
        self.assertEqual(kind_control_plane_host("kind-popot-ax"),
                         "popot-ax-control-plane")

    def test_uses_ax_defaults_when_provider_and_model_are_unset(self):
        self.assertEqual(selected_provider_model({}), ("openrouter", "openrouter/free"))
        self.assertEqual(
            selected_provider_model({"AX_LOCAL_PROVIDER": "", "AX_LOCAL_MODEL": ""}),
            ("openrouter", "openrouter/free"),
        )

    def test_explicit_provider_and_model_override_ax_defaults(self):
        self.assertEqual(
            selected_provider_model({"AX_LOCAL_PROVIDER": "nous", "AX_LOCAL_MODEL": "custom-model"}),
            ("nous", "custom-model"),
        )

    def test_other_provider_requires_its_own_model(self):
        with self.assertRaisesRegex(ValueError, "AX_LOCAL_MODEL"):
            selected_provider_model({"AX_LOCAL_PROVIDER": "nous"})

    def test_roles_keep_tools_and_use_the_local_agent(self):
        environment = {
            "AX_LOCAL_WORKER_IMAGE": "localhost:5001/popot-agent-ax@sha256:" + "a" * 64,
            "AX_LOCAL_MODEL": "selected-model",
            "AX_LOCAL_PROVIDER": "openrouter",
            "AX_LOCAL_BASE_URL": "http://172.18.0.5:8004/v1",
            "AX_LOCAL_PROXY_TOKEN": "session-capability",
            "AX_LOCAL_SESSION_BACKEND": "file",
        }
        with patch.dict(os.environ, environment), patch("ax_local.api.server.create_server") as create:
            make_server()
        roles = create.call_args.kwargs["roles"]
        self.assertEqual(roles["backend_engineer"]["agent"], "openrouter")
        self.assertIn("bash", roles["backend_engineer"]["tools"])
        self.assertEqual(set(create.call_args.args[0]), {"openrouter"})

    def test_container_bind_host_is_configurable(self):
        environment = {
            "AX_LOCAL_WORKER_IMAGE": "localhost:5001/popot-agent-ax@sha256:" + "a" * 64,
            "AX_LOCAL_MODEL": "selected-model",
            "AX_LOCAL_PROVIDER": "openrouter",
            "AX_LOCAL_BASE_URL": "http://172.18.0.5:8004/v1",
            "AX_LOCAL_PROXY_TOKEN": "session-capability",
            "AX_LOCAL_SESSION_BACKEND": "file",
            "AX_LOCAL_BIND_HOST": "0.0.0.0",
        }
        with patch.dict(os.environ, environment), patch("ax_local.api.server.create_server") as create:
            make_server()
        self.assertEqual(create.call_args.kwargs["host"], "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
