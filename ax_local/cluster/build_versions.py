"""Read the shared Docker/local source pins without evaluating shell code."""

from pathlib import Path
import re


DEFAULT_PATH = Path(__file__).resolve().parents[1] / 'docker/versions.env'


def load_build_versions(path=None):
    versions = {}
    expected = {'AX_VERSION', 'AX_COMMIT', 'SUBSTRATE_COMMIT', 'KO_VERSION'}
    for number, line in enumerate(Path(path or DEFAULT_PATH).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Z_]+)=([A-Za-z0-9.+_-]+)', line)
        if not match or match[1] not in expected or match[1] in versions:
            raise ValueError(f'Invalid build version assignment on line {number}')
        versions[match[1]] = match[2]
    if set(versions) != expected:
        raise ValueError('Build versions must define AX_VERSION, AX_COMMIT, SUBSTRATE_COMMIT and KO_VERSION')
    for key, value in versions.items():
        pattern = r'[0-9a-f]{40}' if key.endswith('_COMMIT') else r'v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?'
        if not re.fullmatch(pattern, value):
            raise ValueError(f'Invalid build version: {key}')
    return versions


BUILD_VERSIONS = load_build_versions()
