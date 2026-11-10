"""Perform file operations as the workspace user, without model credentials."""

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from urllib import request

# The tool runs with a stripped environment from /workspace. Resolve only our
# installed code; do not import packages supplied by the writable workspace.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from popot_agents.runtime_config import RUNTIME
from popot_agents.tools import MAX_TOOL_OUTPUT, WORKSPACE_ROOT, _public_url, workspace_path


def _atomic_write(path, chunks) -> None:
    path.parent.mkdir(mode=0o770, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            for chunk in chunks:
                file.write(chunk)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


GUIDANCE_DOCUMENTS = ('AGENTS.md', 'CLAUDE.md', 'GLOSSARY.md', 'GLOSSARY-MAP.md', 'docs/agents/domain.md')
MAX_REPOSITORIES = 32
MAX_DISCOVERY_ERRORS = 32


def _discover_repositories(root: Path) -> tuple[list[Path], dict, bool]:
    """List the checkouts under root, recording bounded discovery errors."""
    if (root / '.git').exists():
        return [root], {}, False
    repositories = []
    errors = {}
    truncated = False
    for path in root.iterdir():
        try:
            if not path.is_symlink() and path.is_dir() and (path / '.git').exists():
                repositories.append(path)
        except (OSError, RuntimeError) as exc:
            if len(errors) < MAX_DISCOVERY_ERRORS:
                errors[str(path.relative_to(root))] = type(exc).__name__
            else:
                truncated = True
    return sorted(repositories), errors, truncated


def _read_document(repository: Path, path: Path, limit: int) -> dict:
    """Read one guidance document that must resolve inside the checkout."""
    try:
        if not path.resolve().is_relative_to(repository):
            return {'error': 'document points outside the repository'}
        if not path.is_file():
            return {}
        with path.open(encoding='utf-8', newline='') as source:
            content = source.read(limit + 1)
    except (OSError, UnicodeError, RuntimeError) as exc:
        return {'error': type(exc).__name__}
    truncated = len(content) > limit
    return {'content': content[:limit], 'truncated': truncated,
            'next_offset': limit if truncated else None}


def _read_documents(repository: Path, root: Path) -> dict:
    """Read every guidance document of a single checkout."""
    limit = MAX_TOOL_OUTPUT // len(GUIDANCE_DOCUMENTS)
    documents = {}
    for name in GUIDANCE_DOCUMENTS:
        path = repository / name
        document = _read_document(repository, path, limit)
        if document:
            documents[str(path.relative_to(root))] = document
    return documents


def _read_adrs(repository: Path, root: Path) -> tuple[list, dict]:
    """List the ADR files that resolve inside the checkout."""
    directory = repository / 'docs/adr'
    try:
        if directory.resolve().is_relative_to(repository) and directory.is_dir():
            adrs = sorted(str(path.relative_to(root)) for path in directory.glob('*.md')
                          if path.is_file() and path.resolve().is_relative_to(repository))
            return adrs, {}
    except (OSError, RuntimeError) as exc:
        return [], {str(directory.relative_to(root)): {'error': type(exc).__name__}}
    return [], {}


def repository_context() -> dict:
    """Return the guidance of the checkout the worker is running in."""
    try:
        root = WORKSPACE_ROOT.resolve()
        if not root.is_dir():
            return {}
        repositories, errors, errors_truncated = _discover_repositories(root)
    except (OSError, RuntimeError) as exc:
        return {'discovery_errors': {'.': type(exc).__name__}}
    discovery = {'discovery_errors': errors, 'discovery_errors_truncated': errors_truncated} if errors else {}
    if not repositories:
        return discovery
    if len(repositories) != 1:
        listed = [str(path.relative_to(root)) for path in repositories[:MAX_REPOSITORIES]]
        return {**discovery, 'repositories': listed, 'selection_required': True,
                'truncated': len(repositories) > MAX_REPOSITORIES}
    repository = repositories[0]
    documents = _read_documents(repository, root)
    adrs, adr_errors = _read_adrs(repository, root)
    documents.update(adr_errors)
    if not documents and not adrs:
        return discovery
    return {**discovery, 'repository': str(repository.relative_to(root)), 'documents': documents,
            'adrs': adrs[:MAX_REPOSITORIES], 'adrs_truncated': len(adrs) > MAX_REPOSITORIES}


def main() -> None:
    action = sys.argv[1]
    if action == 'save_output':
        if (WORKSPACE_ROOT / '.tool-results').is_symlink():
            raise ValueError('saved tool output directory must not be a symlink')
        path = workspace_path(f'.tool-results/{uuid.uuid4().hex}.txt')
        sys.stdin.reconfigure(encoding='utf-8', errors='replace', newline='')
        _atomic_write(path, (chunk.encode('utf-8') for chunk in iter(lambda: sys.stdin.read(65536), '')))
        sys.stdout.write(str(path.relative_to(WORKSPACE_ROOT.resolve())))
        return
    arguments = json.load(sys.stdin)
    if action == 'repository_context':
        sys.stdout.write(json.dumps(repository_context(), ensure_ascii=False))
        return
    if action == 'export_artifacts':
        from popot_agents.artifacts import collect_artifacts
        sys.stdout.write(json.dumps(collect_artifacts(WORKSPACE_ROOT), ensure_ascii=False))
        return
    path = workspace_path(arguments["path"])
    if action == 'read_tool_output' and ((WORKSPACE_ROOT / '.tool-results').is_symlink()
            or (WORKSPACE_ROOT / arguments['path']).is_symlink()):
        raise ValueError('saved tool output must not be a symlink')
    if action in {"read", "read_tool_output"}:
        offset, limit = arguments.get('offset', 0), arguments.get('limit', MAX_TOOL_OUTPUT)
        with path.open(encoding="utf-8", newline="") as file:
            remaining = offset
            while remaining:
                skipped = file.read(min(remaining, 65536))
                if not skipped:
                    break
                remaining -= len(skipped)
            content = file.read(limit + 1)
        sys.stdout.write(content[:limit])
        if len(content) > limit:
            reader = 'read_tool_output' if action == 'read_tool_output' else 'read_file'
            sys.stdout.write(f"\n[output truncated; continue with {reader} offset={offset + limit} limit={limit}]")
    elif action == "write":
        _atomic_write(path, [arguments["content"].encode("utf-8")])
        sys.stdout.write(f"wrote {path.relative_to(WORKSPACE_ROOT.resolve())}")
    elif action == "download":
        url = _public_url(arguments["url"])
        with request.urlopen(url, timeout=RUNTIME["timeouts"]["file_download_seconds"]) as response:
            _public_url(response.geturl())
            content = response.read(RUNTIME["limits"]["download_bytes"] + 1)
        if len(content) > RUNTIME["limits"]["download_bytes"]:
            raise ValueError(f"download exceeds {RUNTIME['limits']['download_bytes']} bytes")
        _atomic_write(path, [content])
        sys.stdout.write(f"downloaded {len(content)} bytes to {path.relative_to(WORKSPACE_ROOT.resolve())}")
    else:
        raise ValueError("unknown file action")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, UnicodeError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from exc
