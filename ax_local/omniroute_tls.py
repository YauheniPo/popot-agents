"""Issue private local OmniRoute certificates and export public client trust."""

import argparse
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CA_KEY = 'ca/key.pem'
CA_CERT = 'trust/ca.pem'
CA_BUNDLE = 'trust/ca-bundle.pem'
SERVER_KEY = 'server/key.pem'
SERVER_CERT = 'server/cert.pem'
SERVER_CA = 'server/ca.pem'
FILES = (CA_KEY, CA_CERT, CA_BUNDLE, SERVER_KEY, SERVER_CERT, SERVER_CA)
CERT_CONFIG = '''[req]
distinguished_name = dn
[dn]
[ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
[server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
subjectAltName = DNS:omniroute,DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1,IP:::1
'''


def openssl(*arguments):
    subprocess.run(['openssl', *map(str, arguments)], check=True, capture_output=True, timeout=30)


def issue(directory):
    config = directory / 'openssl.cnf'
    config.write_text(CERT_CONFIG)
    openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '3650',
            '-subj', '/CN=Popot local OmniRoute CA', '-config', config, '-extensions', 'ca',
            '-keyout', directory / CA_KEY, '-out', directory / CA_CERT)
    openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=omniroute',
            '-config', config, '-keyout', directory / SERVER_KEY, '-out', directory / 'server.csr')
    openssl('x509', '-req', '-in', directory / 'server.csr', '-days', '365',
            '-CA', directory / CA_CERT, '-CAkey', directory / CA_KEY,
            '-CAcreateserial', '-CAserial', directory / 'serial',
            '-extfile', config, '-extensions', 'server', '-out', directory / SERVER_CERT)
    shutil.copyfile(directory / CA_CERT, directory / SERVER_CA)
    public_roots = ssl.get_default_verify_paths().cafile
    if not public_roots:
        raise ValueError('system CA certificates are required')
    bundle = Path(public_roots).read_bytes() + b'\n' + (directory / CA_CERT).read_bytes()
    (directory / CA_BUNDLE).write_bytes(bundle)


def initialize(root):
    root = Path(root)
    present = [(root / name).is_file() for name in FILES]
    if any(present) and not all(present):
        raise ValueError('incomplete OmniRoute TLS state; restore the existing certificate set')
    if not any(present):
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            directory = Path(temporary)
            for name in ('ca', 'trust', 'server'):
                (directory / name).mkdir(mode=0o700)
            issue(directory)
            for name in FILES:
                target = root / name
                target.parent.mkdir(mode=0o700, exist_ok=True)
                (directory / name).replace(target)
    openssl('verify', '-CAfile', root / CA_CERT, root / SERVER_CERT)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(root / SERVER_CERT, root / SERVER_KEY)
    os.chmod(root, 0o755)
    os.chmod(root / 'ca', 0o700)
    os.chmod(root / CA_KEY, 0o600)
    os.chmod(root / 'trust', 0o755)
    for name in (CA_CERT, CA_BUNDLE):
        os.chmod(root / name, 0o644)
    for name in ('server', SERVER_KEY, SERVER_CERT, SERVER_CA):
        os.chown(root / name, 1000, 1000)  # The pinned OmniRoute image runs as node.
        os.chmod(root / name, 0o700 if name == 'server' else 0o400)


def export_trust(root=ROOT):
    root = Path(root)
    result = subprocess.run(['bash', str(root / 'ax_local/compose.sh'), 'run', '--rm', '--no-deps',
        '--entrypoint', 'cat', 'omniroute-init', '/run/popot-tls/ca.pem'],
        text=True, capture_output=True, check=True, timeout=60)
    if 'PRIVATE KEY' in result.stdout:
        raise ValueError('only public CA certificates may be exported')
    ssl.create_default_context(cadata=result.stdout)
    path = root / 'ax_local/.local/omniroute-ca.pem'
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_text(result.stdout)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'export'))
    parser.add_argument('--directory', type=Path, default=Path('/tls'))
    args = parser.parse_args()
    if args.action == 'init':
        initialize(args.directory)
    else:
        export_trust()


if __name__ == '__main__':
    main()
