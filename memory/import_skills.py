"""Upsert repository SKILL.md files into a Cognee brain through its skills API."""

import argparse
import json
import os
from pathlib import Path
import re


def sync_skills(root: Path, client, dataset: str = 'popot-agents') -> int:
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', dataset):
        raise ValueError('dataset must be a project slug')
    if root.is_symlink() or not root.is_dir():
        raise ValueError('skills root must be a directory without symlinks')
    for parent, dirs, files in os.walk(root):
        if any((Path(parent) / name).is_symlink() for name in dirs + files):
            raise ValueError('skills cannot contain symlinks')

    payloads, names = [], set()
    for source in sorted(root.rglob('SKILL.md')):
        relative = source.parent.relative_to(root).as_posix()
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*', relative):
            raise ValueError(f'invalid skill directory: {relative}')
        name = relative.replace('/', '--')
        if name in names:
            raise ValueError(f'duplicate skill slug: {name}')
        names.add(name)
        text = source.read_text(encoding='utf-8')
        if not text.strip():
            raise ValueError(f'empty SKILL.md: {relative}')
        text += f'\n\n## Repository source\nskills/{relative}/SKILL.md\n'
        for reference in sorted(source.parent.rglob('*.md')):
            if reference == source:
                continue
            label = reference.relative_to(source.parent).as_posix()
            text += f'\n\n## Bundled reference: {label}\n\n' + reference.read_text(encoding='utf-8')
        if len(text.encode('utf-8')) > 100000:
            raise ValueError(f'skill exceeds 100000 UTF-8 bytes: {relative}')
        payloads.append({'dataset_name': dataset, 'skill_name': name, 'skills_text': text})
    if not payloads:
        raise ValueError('no SKILL.md files found')

    for index, payload in enumerate(payloads, 1):
        print(f"Cognee skills [{index}/{len(payloads)}] {payload['skill_name']}: indexing", flush=True)
        result = client._request('/api/v1/skills', json.dumps(payload).encode('utf-8'))
        items = result.get('items', [])
        saved = isinstance(items, list) and any(
            isinstance(item, dict) and item.get('kind') == 'skill'
            and item.get('name') == payload['skill_name'] for item in items)
        if result.get('status') != 'completed' or not saved:
            raise RuntimeError(f"Skill ingestion did not complete: {payload['skill_name']}")
    return len(payloads)


def main() -> None:
    # The existing authenticated adapter is mounted beside this script in Cognee.
    from cognee_memory import CogneeMemory
    from popot_agents.runtime_config import RUNTIME

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/app/popot-skills'))
    parser.add_argument('--dataset', default='popot-agents')
    args = parser.parse_args()
    client = CogneeMemory('http://127.0.0.1:8000', os.environ['DEFAULT_USER_PASSWORD'],
                          timeout=RUNTIME['timeouts']['memory_skill_import_seconds'])
    count = sync_skills(args.root, client, args.dataset)
    print(f'Cognee skills: {count} imported into {args.dataset}')


if __name__ == '__main__':
    main()
