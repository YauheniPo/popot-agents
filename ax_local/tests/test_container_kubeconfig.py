import unittest
from unittest.mock import patch

from ax_local.cluster.container_kubeconfig import rewrite_for_container


class ContainerKubeconfigTests(unittest.TestCase):
    def test_rewrites_only_local_kind_endpoint_and_preserves_credentials(self):
        config = {
            "current-context": "kind-popot-ax",
            "contexts": [{"name": "kind-popot-ax", "context": {
                "cluster": "kind-popot-ax", "user": "kind-popot-ax",
            }}],
            "clusters": [{"name": "kind-popot-ax", "cluster": {
                "server": "https://127.0.0.1:54931",
                "certificate-authority-data": "encoded-ca",
            }}],
            "users": [{"name": "kind-popot-ax", "user": {
                "client-certificate-data": "encoded-cert",
                "client-key-data": "encoded-key",
            }}],
        }

        result = rewrite_for_container(config)

        self.assertEqual(result["clusters"][0]["cluster"]["server"],
                         "https://popot-ax-control-plane:6443")
        self.assertEqual(result["clusters"][0]["cluster"]["tls-server-name"],
                         "127.0.0.1")
        self.assertEqual(result["users"], config["users"])
        self.assertEqual(config["clusters"][0]["cluster"]["server"],
                         "https://127.0.0.1:54931")

    def test_rejects_nonlocal_cluster_endpoint(self):
        config = {
            "current-context": "kind-popot-ax",
            "contexts": [{"name": "kind-popot-ax", "context": {
                "cluster": "kind-popot-ax",
            }}],
            "clusters": [{"name": "kind-popot-ax", "cluster": {
                "server": "https://production.example:443",
            }}],
        }
        with self.assertRaises(ValueError):
            rewrite_for_container(config)

    def test_container_endpoint_follows_configured_kind_context(self):
        config = {
            "current-context": "kind-test",
            "contexts": [{"name": "kind-test", "context": {"cluster": "kind-test"}}],
            "clusters": [{"name": "kind-test", "cluster": {
                "server": "https://127.0.0.1:51093",
            }}],
        }
        with patch("ax_local.cluster.container_kubeconfig.CONTEXT", "kind-test"):
            result = rewrite_for_container(config)
        self.assertEqual(result["clusters"][0]["cluster"]["server"],
                         "https://test-control-plane:6443")


if __name__ == "__main__":
    unittest.main()
