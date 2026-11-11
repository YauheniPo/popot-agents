"""Bounded deliverables, stored with the session before sandbox cleanup."""
import base64
import binascii
import hashlib
import os
import re
import stat
from pathlib import Path

from popot_agents.runtime_config import RUNTIME

MAX_ARTIFACTS = 16
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}')
_ERRORS = {
    'count_limit': f'At most {MAX_ARTIFACTS} artifacts can be handed off.',
    'byte_limit': 'Text artifacts exceed the handoff byte limit.',
    'total_byte_limit': 'Artifacts exceed the combined handoff byte limit.',
    'invalid_artifact': 'Invalid artifact name or content.',
    'checksum': 'Artifact checksum mismatch.',
    'directory': 'Artifact directory must be a readable directory without symlinks.',
    'name': 'Artifact names must be plain non-hidden filenames.',
    'regular_file': 'Artifacts must be regular files.',
    # Accept diagnostics from older workers during a rolling rebuild.
    'readable_utf8': 'Artifacts must be readable UTF-8 files without symlinks.',
    'readable_file': 'Artifacts must be readable files without symlinks.',
}


def artifact_error_message(code, minimum=None):
    """Render approved diagnostics without forwarding file data or worker stderr."""
    if not isinstance(code, str) or code not in _ERRORS:
        return None
    message = _ERRORS[code]
    if code == 'byte_limit':
        message += f" Combined UTF-8 limit: {RUNTIME['limits']['tool_output_chars']} bytes."
    if code == 'total_byte_limit':
        message += f" Combined file limit: {RUNTIME['limits']['artifact_bytes']} bytes before base64 encoding."
    if code in {'byte_limit', 'total_byte_limit', 'count_limit'}:
        if type(minimum) is int and minimum > 0:
            unit = 'files' if code == 'count_limit' else 'bytes'
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
    total = text_total = 0
    names = set()
    for item in artifacts:
        if (not isinstance(item, dict) or set(item) not in (
                {'name', 'content', 'sha256'}, {'name', 'content', 'sha256', 'encoding'})
                or not isinstance(item['name'], str) or not _NAME.fullmatch(item['name'])
                or item['name'] in names or not isinstance(item['content'], str)):
            raise ArtifactError('invalid_artifact')
        if 'encoding' in item:
            if item['encoding'] != 'base64':
                raise ArtifactError('invalid_artifact')
            remaining = RUNTIME['limits']['artifact_bytes'] - total
            if len(item['content']) > 4 * ((remaining + 2) // 3):
                raise ArtifactError('total_byte_limit')
            try:
                data = base64.b64decode(item['content'], validate=True)
            except (ValueError, binascii.Error):
                raise ArtifactError('invalid_artifact') from None
        else:
            data = item['content'].encode('utf-8')
            text_total += len(data)
            if text_total > RUNTIME['limits']['tool_output_chars']:
                raise ArtifactError('byte_limit', text_total)
        total += len(data)
        if total > RUNTIME['limits']['artifact_bytes']:
            raise ArtifactError('total_byte_limit', total)
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
        artifacts, remaining = [], RUNTIME['limits']['artifact_bytes']
        for name in sorted(names):
            if not _NAME.fullmatch(name):
                raise ArtifactError('name')
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, 'rb') as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise ArtifactError('regular_file')
                data = file.read(remaining + 1)
            if len(data) > remaining:
                raise ArtifactError('total_byte_limit', RUNTIME['limits']['artifact_bytes'] - remaining + len(data))
            artifact = {'name': name, 'sha256': hashlib.sha256(data).hexdigest()}
            try:
                artifact['content'] = data.decode('utf-8')
            except UnicodeDecodeError:
                artifact.update(content=base64.b64encode(data).decode('ascii'), encoding='base64')
            artifacts.append(artifact)
            remaining -= len(data)
        return validate_artifacts(artifacts)
    except OSError:
        raise ArtifactError('readable_file') from None
    finally:
        os.close(directory)
