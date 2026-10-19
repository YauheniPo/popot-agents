"""Read shared pins from pyproject.toml; emit shell assignments or run Compose."""

from pathlib import Path
import os
import re
import shlex
import sys

if sys.version_info < (3, 11):
    raise SystemExit('AX build tools require Python 3.11 or newer; use it as python3 in PATH')

import tomllib


DEFAULT_PATH = Path(__file__).resolve().parents[2] / 'pyproject.toml'
RELEASE = r'v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?'
TAG = r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}'
IMAGE = rf'[A-Za-z0-9][A-Za-z0-9._:/-]*:{TAG}(?:@sha256:[0-9a-f]{{64}})?'
PATTERNS = {
    'AX_VERSION': RELEASE, 'AX_COMMIT': r'[0-9a-f]{40}',
    'SUBSTRATE_COMMIT': r'[0-9a-f]{40}', 'KO_VERSION': RELEASE,
    'KUBECTL_VERSION': RELEASE, 'OMNIROUTE_VERSION': TAG, 'HOLMES_VERSION': TAG,
    'COGNEE_VERSION': r'main-[0-9a-f]{7}', 'COGNEE_COMMIT': r'[0-9a-f]{40}',
    'COGNEE_SOURCE_SHA256': r'[0-9a-f]{64}',
    'POSTGRES_VERSION': TAG, 'NGINX_VERSION': TAG,
    'PYTHON_IMAGE': IMAGE, 'GO_IMAGE': IMAGE, 'NODE_IMAGE': IMAGE,
}


def load_build_versions(path=None):
    with Path(path or DEFAULT_PATH).open('rb') as stream:
        config = tomllib.load(stream)
    versions = config.get('tool', {}).get('popot', {}).get('build', {})
    if not isinstance(versions, dict) or set(versions) != set(PATTERNS):
        raise ValueError('[tool.popot.build] must define exactly: ' + ', '.join(PATTERNS))
    for key, value in versions.items():
        if not isinstance(value, str) or not re.fullmatch(PATTERNS[key], value):
            raise ValueError(f'Invalid build version: {key}')
    if versions['COGNEE_VERSION'] != 'main-' + versions['COGNEE_COMMIT'][:7]:
        raise ValueError('COGNEE_VERSION must match COGNEE_COMMIT')
    return versions


BUILD_VERSIONS = load_build_versions()


if __name__ == '__main__':
    if sys.argv[1:2] == ['compose']:
        os.execvpe('docker', ['docker', *sys.argv[1:]], {**os.environ, **BUILD_VERSIONS})
    if len(sys.argv) != 1:
        raise SystemExit('Usage: build_versions.py [compose <docker compose arguments>]')
    for key, value in BUILD_VERSIONS.items():
        print(f'{key}={shlex.quote(value)}')
