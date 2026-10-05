"""Perform file operations as the workspace user, without model credentials."""

import json
import os
import sys
import tempfile
from pathlib import Path
from urllib import request

# The tool runs with a stripped environment from /workspace. Resolve only our
# installed code; do not import packages supplied by the writable workspace.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from popot_agents.runtime_config import RUNTIME
from popot_agents.tools import MAX_TOOL_OUTPUT, WORKSPACE_ROOT, _public_url, workspace_path


def _atomic_write(path, content: bytes) -> None:
    path.parent.mkdir(mode=0o770, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    action = sys.argv[1]
    arguments = json.load(sys.stdin)
    path = workspace_path(arguments["path"])
    if action == "read":
        with path.open(encoding="utf-8") as file:
            content = file.read(MAX_TOOL_OUTPUT + 1)
        sys.stdout.write(content[:MAX_TOOL_OUTPUT])
        if len(content) > MAX_TOOL_OUTPUT:
            sys.stdout.write("\n[output truncated]")
    elif action == "write":
        _atomic_write(path, arguments["content"].encode("utf-8"))
        sys.stdout.write(f"wrote {path.relative_to(WORKSPACE_ROOT.resolve())}")
    elif action == "download":
        url = _public_url(arguments["url"])
        with request.urlopen(url, timeout=RUNTIME["timeouts"]["file_download_seconds"]) as response:
            _public_url(response.geturl())
            content = response.read(RUNTIME["limits"]["download_bytes"] + 1)
        if len(content) > RUNTIME["limits"]["download_bytes"]:
            raise ValueError(f"download exceeds {RUNTIME['limits']['download_bytes']} bytes")
        _atomic_write(path, content)
        sys.stdout.write(f"downloaded {len(content)} bytes to {path.relative_to(WORKSPACE_ROOT.resolve())}")
    else:
        raise ValueError("unknown file action")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, UnicodeError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from exc
