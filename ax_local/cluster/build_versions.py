"""Read shared build pins from pyproject.toml; emit validated shell assignments."""

from pathlib import Path
import re
import shlex
import sys

if sys.version_info < (3, 11):
    raise SystemExit('AX build tools require Python 3.11 or newer; use it as python3 in PATH')

import tomllib


DEFAULT_PATH = Path(__file__).resolve().parents[2] / 'pyproject.toml'


def load_build_versions(path=None):
    with Path(path or DEFAULT_PATH).open('rb') as stream:
        config = tomllib.load(stream)
    versions = config.get('tool', {}).get('popot', {}).get('build', {})
    expected = {'AX_VERSION', 'AX_COMMIT', 'SUBSTRATE_COMMIT', 'KO_VERSION'}
    if not isinstance(versions, dict) or set(versions) != expected:
        raise ValueError('[tool.popot.build] must define AX_VERSION, AX_COMMIT, SUBSTRATE_COMMIT and KO_VERSION')
    for key, value in versions.items():
        pattern = r'[0-9a-f]{40}' if key.endswith('_COMMIT') else r'v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?'
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError(f'Invalid build version: {key}')
    return versions


BUILD_VERSIONS = load_build_versions()


if __name__ == '__main__':
    for key, value in BUILD_VERSIONS.items():
        print(f'{key}={shlex.quote(value)}')
