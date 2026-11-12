"""Bounded text deliverables, stored with the session before sandbox cleanup."""
import hashlib
import os
import re
import stat
from pathlib import Path

from popot_agents.runtime_config import RUNTIME

MAX_ARTIFACTS = 16
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}')
_ERRORS = {
    'count_limit': f'At most {MAX_ARTIFACTS} text artifacts can be handed off.',
    'byte_limit': 'Text artifacts exceed the handoff byte limit.',
    'invalid_artifact': 'Invalid artifact name or content.',
    'checksum': 'Artifact checksum mismatch.',
    'directory': 'Artifact directory must be a readable directory without symlinks.',
    'name': 'Artifact names must be plain non-hidden filenames.',
    'regular_file': 'Artifacts must be regular text files.',
    'readable_utf8': 'Artifacts must be readable UTF-8 files without symlinks.',
}


def artifact_error_message(code, minimum=None):
    """Render approved diagnostics without forwarding file data or worker stderr."""
    if not isinstance(code, str) or code not in _ERRORS:
        return None
    message = _ERRORS[code]
    if code == 'byte_limit':
        message += f" Combined UTF-8 limit: {RUNTIME['limits']['tool_output_chars']} bytes."
    if code in {'byte_limit', 'count_limit'}:
        if type(minimum) is int and minimum > 0:
            unit = 'bytes' if code == 'byte_limit' else 'files'
            message += f' Observed at least {minimum} {unit}.'
        message += ' Move research inputs, raw logs and duplicate copies outside /workspace/artifacts; keep only final deliverables.'
    return message


class ArtifactError(ValueError):
    def __init__(self, code, minimum=None):
        self.code, self.minimum = code, minimum
        super().__init__(artifact_error_message(code, minimum))


def validate_artifacts(artifacts):
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
        raise ArtifactError('count_limit', len(artifacts) if isinstance(artifacts, list) else None)
    total, names = 0, set()
    for item in artifacts:
        if (not isinstance(item, dict) or set(item) != {'name', 'content', 'sha256'}
                or not isinstance(item['name'], str) or not _NAME.fullmatch(item['name'])
                or item['name'] in names or not isinstance(item['content'], str)):
            raise ArtifactError('invalid_artifact')
        data = item['content'].encode('utf-8')
        total += len(data)
        if total > RUNTIME['limits']['tool_output_chars']:
            raise ArtifactError('byte_limit', total)
        if item['sha256'] != hashlib.sha256(data).hexdigest():
            raise ArtifactError('checksum')
        names.add(item['name'])
    return artifacts


def collect_artifacts(workspace):
    """Read only the explicit output directory, without following symlinks."""
    try:
        directory = os.open(Path(workspace) / 'artifacts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    except OSError:
        raise ArtifactError('directory') from None
    try:
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > MAX_ARTIFACTS:
                    raise ArtifactError('count_limit', len(names))
        artifacts, remaining = [], RUNTIME['limits']['tool_output_chars']
        for name in sorted(names):
            if not _NAME.fullmatch(name):
                raise ArtifactError('name')
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, 'rb') as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise ArtifactError('regular_file')
                data = file.read(remaining + 1)
            if len(data) > remaining:
                raise ArtifactError('byte_limit', RUNTIME['limits']['tool_output_chars'] - remaining + len(data))
            artifacts.append({'name': name, 'content': data.decode('utf-8'),
                              'sha256': hashlib.sha256(data).hexdigest()})
            remaining -= len(data)
        return validate_artifacts(artifacts)
    except (OSError, UnicodeError):
        raise ArtifactError('readable_utf8') from None
    finally:
        os.close(directory)
