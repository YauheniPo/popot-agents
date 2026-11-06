"""Bounded text deliverables, stored with the session before sandbox cleanup."""
import hashlib
import os
import re
import stat
from pathlib import Path

from popot_agents.runtime_config import RUNTIME

MAX_ARTIFACTS = 16
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}')


class ArtifactError(ValueError):
    pass


def validate_artifacts(artifacts):
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
        raise ArtifactError('At most 16 text artifacts can be handed off')
    total, names = 0, set()
    for item in artifacts:
        if (not isinstance(item, dict) or set(item) != {'name', 'content', 'sha256'}
                or not isinstance(item['name'], str) or not _NAME.fullmatch(item['name'])
                or item['name'] in names or not isinstance(item['content'], str)):
            raise ArtifactError('Invalid artifact name or content')
        data = item['content'].encode('utf-8')
        total += len(data)
        if total > RUNTIME['limits']['tool_output_chars']:
            raise ArtifactError('Text artifacts exceed the handoff byte limit')
        if item['sha256'] != hashlib.sha256(data).hexdigest():
            raise ArtifactError('Artifact checksum mismatch')
        names.add(item['name'])
    return artifacts


def collect_artifacts(workspace):
    """Read only the explicit output directory, without following symlinks."""
    try:
        directory = os.open(Path(workspace) / 'artifacts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    except OSError:
        raise ArtifactError('Artifact directory must be a readable directory without symlinks') from None
    try:
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > MAX_ARTIFACTS:
                    raise ArtifactError('At most 16 text artifacts can be handed off')
        artifacts, remaining = [], RUNTIME['limits']['tool_output_chars']
        for name in sorted(names):
            if not _NAME.fullmatch(name):
                raise ArtifactError('Artifact names must be plain non-hidden filenames')
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, 'rb') as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise ArtifactError('Artifacts must be regular text files')
                data = file.read(remaining + 1)
            if len(data) > remaining:
                raise ArtifactError('Text artifacts exceed the handoff byte limit')
            artifacts.append({'name': name, 'content': data.decode('utf-8'),
                              'sha256': hashlib.sha256(data).hexdigest()})
            remaining -= len(data)
        return validate_artifacts(artifacts)
    except (OSError, UnicodeError):
        raise ArtifactError('Artifacts must be readable UTF-8 files without symlinks') from None
    finally:
        os.close(directory)
