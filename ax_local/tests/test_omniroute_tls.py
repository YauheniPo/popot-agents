"""Local OmniRoute TLS must verify both the CA and the endpoint hostname."""

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib import error, request

from ax_local.omniroute import Api


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL is required to issue local certificates')
class OmniRouteTLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ax_local.omniroute_tls import initialize
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        with patch('ax_local.omniroute_tls.os.chown'):
            initialize(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_certificates_are_private_and_initialization_preserves_the_existing_ca(self):
        from ax_local.omniroute_tls import initialize
        before = (self.root / 'trust/ca.pem').read_bytes()
        with patch('ax_local.omniroute_tls.os.chown'):
            initialize(self.root)
        self.assertEqual(before, (self.root / 'trust/ca.pem').read_bytes())
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'ca').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'trust').stat().st_mode & 0o777, 0o700)
        for name in ('ca.pem', 'ca-bundle.pem'):
            self.assertEqual((self.root / 'trust' / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'server').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'server/key.pem').stat().st_mode & 0o777, 0o400)
        self.assertEqual((self.root / 'ca/key.pem').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('PRIVATE KEY', (self.root / 'trust/ca-bundle.pem').read_text())

    def test_incomplete_certificate_state_is_rejected_without_replacing_trust(self):
        from ax_local.omniroute_tls import initialize
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'trust').mkdir()
            (root / 'trust/ca.pem').write_text('preserve existing trust')
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                initialize(root)
            self.assertEqual((root / 'trust/ca.pem').read_text(), 'preserve existing trust')

    def test_verified_https_keeps_management_cookies_and_rejects_untrusted_certificates(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Set-Cookie', 'session=tls-test; Secure; HttpOnly; Path=/')
                self.end_headers()
                self.wfile.write(b'{}')

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'cookie': self.headers.get('Cookie')}).encode())

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.root / 'server/cert.pem', self.root / 'server/key.pem')
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'https://127.0.0.1:{server.server_port}'
            with patch.dict(os.environ, {'SSL_CERT_FILE': str(self.root / 'trust/ca-bundle.pem')}):
                api = Api(url)
                api.request('POST', '/api/auth/login', {'password': 'fixture-password'})
                self.assertEqual(api.request('GET', '/api/combos')['cookie'], 'session=tls-test')
            untrusted_context = ssl.create_default_context(cafile=ssl.get_default_verify_paths().cafile)
            untrusted_context.minimum_version = ssl.TLSVersion.TLSv1_2
            untrusted = request.build_opener(request.ProxyHandler({}), request.HTTPSHandler(context=untrusted_context))
            with self.assertRaises(error.URLError):
                untrusted.open(url + '/healthz', timeout=2)
            trusted = ssl.create_default_context(cafile=str(self.root / 'trust/ca.pem'))
            trusted.minimum_version = ssl.TLSVersion.TLSv1_2
            with socket.create_connection(('127.0.0.1', server.server_port), timeout=2) as connection:
                with self.assertRaises(ssl.SSLCertVerificationError):
                    trusted.wrap_socket(connection, server_hostname='unrelated.internal')
            if shutil.which('node'):
                script = Path(__file__).resolve().parents[1] / 'omniroute_healthcheck.mjs'
                result = subprocess.run(['node', str(script)], env={**os.environ,
                    'PORT': str(server.server_port), 'NODE_EXTRA_CA_CERTS': str(self.root / 'trust/ca.pem')},
                    capture_output=True, text=True, timeout=6)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run(['node', str(script)], env={**os.environ,
                    'PORT': str(server.server_port), 'NODE_EXTRA_CA_CERTS': ''},
                    capture_output=True, text=True, timeout=6)
                self.assertNotEqual(result.returncode, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_certificate_identity_excludes_unrelated_hosts(self):
        result = subprocess.run(['openssl', 'x509', '-in', str(self.root / 'server/cert.pem'),
                                 '-noout', '-text'], capture_output=True, text=True, check=True)
        self.assertIn('DNS:omniroute', result.stdout)
        self.assertIn('DNS:host.docker.internal', result.stdout)
        self.assertNotIn('DNS:*.internal', result.stdout)
        self.assertNotIn('DNS:unrelated.internal', result.stdout)

    def test_holmes_trust_bundle_keeps_public_roots_and_adds_the_local_ca(self):
        from ax_local.cluster.holmes import omniroute_ca_bundle
        ca = str(self.root / 'trust/ca.pem')
        with patch.dict(os.environ, {'OMNIROUTE_CA_FILE': ca}):
            bundle = omniroute_ca_bundle()
            self.assertIn((self.root / 'trust/ca.pem').read_text(), bundle)
            ssl.create_default_context(cadata=bundle)
        with patch.dict(os.environ, {'OMNIROUTE_CA_FILE': ca, 'SSL_CERT_FILE': ca}):
            self.assertEqual(omniroute_ca_bundle().strip(), (self.root / 'trust/ca.pem').read_text().strip())

    def test_export_reads_only_public_trust_and_rejects_invalid_output(self):
        from ax_local.omniroute_tls import export_trust
        bundle = (self.root / 'trust/ca.pem').read_text()
        with tempfile.TemporaryDirectory() as directory, \
             patch('ax_local.omniroute_tls.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, bundle)
            path = export_trust(Path(directory))
            self.assertEqual(path.read_text(), bundle)
            self.assertEqual(run.call_args.args[0][-1], '/run/popot-tls/ca.pem')
            for invalid in ('not a certificate', bundle + '\nPRIVATE KEY'):
                run.return_value = subprocess.CompletedProcess([], 0, invalid)
                with self.assertRaises((ValueError, ssl.SSLError)):
                    export_trust(Path(directory))
            self.assertEqual(path.read_text(), bundle)

    def test_cli_initialization_only_uses_the_fixed_private_volume(self):
        from ax_local.omniroute_tls import main
        with patch('sys.argv', ['omniroute_tls', 'init', '--directory', '/tmp/unrelated']), \
             patch('ax_local.omniroute_tls.initialize') as initialize, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                main()
            self.assertEqual(result.exception.code, 2)
            initialize.assert_not_called()
        with patch('sys.argv', ['omniroute_tls', 'init']), \
             patch('ax_local.omniroute_tls.initialize') as initialize:
            main()
            initialize.assert_called_once_with(Path('/tls'))
        with patch('sys.argv', ['omniroute_tls', 'export']), \
             patch('ax_local.omniroute_tls.export_trust') as export:
            main()
            export.assert_called_once_with()

    def test_server_stops_before_importing_omniroute_when_tls_material_is_missing(self):
        if not shutil.which('node'):
            self.skipTest('Node is required for the native startup guard')
        script = Path(__file__).resolve().parents[1] / 'omniroute_start.mjs'
        result = subprocess.run(['node', str(script)], env={**os.environ,
            'OMNIROUTE_TLS_CERT': str(self.root / 'missing.pem'),
            'OMNIROUTE_TLS_KEY': str(self.root / 'server/key.pem')},
            capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('ERR_MODULE_NOT_FOUND', result.stderr)


if __name__ == '__main__':
    unittest.main()
