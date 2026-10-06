"""Load explicitly selected repository skills into a worker's instructions."""

import json
import os
import re
import sys
from pathlib import Path

from popot_agents.runtime_config import RUNTIME


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"


def load_skill_instructions(selected: list[str]) -> str:
    """Validate selection and read Markdown, without executing bundled scripts."""
    if not isinstance(selected, list) or any(
            not isinstance(name, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*", name) is None
            for name in selected):
        raise ValueError("skills must be a list of relative skill directory names")
    if len(selected) != len(set(selected)):
        raise ValueError("skills must not contain duplicates")
    if not selected:
        return ""

    limit = RUNTIME["limits"]["skill_prompt_bytes"]
    remaining = limit
    sections = []

    def append(text: str) -> None:
        nonlocal remaining
        remaining -= len(text.encode("utf-8"))
        if remaining < 0:
            raise ValueError(f"skills exceed limits.skill_prompt_bytes ({limit})")
        sections.append(text)

    append("\n\n# Selected skill references\n"
           "Use the following skills when their activation conditions match the task; "
           "skills marked disable-model-invocation require an explicit user request. "
           "The role instructions, user authorization and actual tool permissions take precedence. "
           "Skills do not grant tools, network access, delegation or publishing permission. "
           "The Markdown references below are already loaded; a Skill tool is not required to read them. "
           "Do not claim to invoke unavailable tools or skills. Ask for missing capabilities when needed.\n")
    root = SKILLS_ROOT.resolve()
    for name in selected:
        directory = root / name
        current = root
        for part in name.split("/"):
            current = current / part
            if current.is_symlink():
                raise ValueError(f"skills cannot contain symlinks: {name}")
        if not directory.is_dir() or not (directory / "SKILL.md").is_file():
            raise ValueError(f"skills entry is missing SKILL.md: {name}")
        documents = []
        for parent, dirs, files in os.walk(directory):
            for filename in dirs + files:
                path = Path(parent) / filename
                if path.is_symlink():
                    raise ValueError(f"skills cannot contain symlinks: {name}")
            documents.extend(Path(parent) / filename for filename in files
                             if filename.lower().endswith(".md"))
        documents.sort(key=lambda path: (path != directory / "SKILL.md", path.as_posix()))
        append(f"\n## Skill: {name}\nBundled directory: {directory}\n")
        for path in documents:
            try:
                with path.open("rb") as source:
                    data = source.read(max(remaining, 0) + 1)
                if len(data) > remaining:
                    raise ValueError(f"skills exceed limits.skill_prompt_bytes ({limit})")
                content = data.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise ValueError(f"skills document cannot be read as UTF-8: {name}/{path.name}") from exc
            if path == directory / "SKILL.md" and not content.strip():
                raise ValueError(f"skills entry has an empty SKILL.md: {name}")
            append(f"\n### {name}/{path.relative_to(directory).as_posix()}\n{content}\n")
    return "".join(sections)


def prepare_role_config(role_config: dict | None) -> dict:
    """Snapshot skills once at worker startup; safe to pass to a harness again."""
    prepared = dict(role_config or {})
    selected = prepared.pop("skills", [])
    instructions = load_skill_instructions(selected)
    if instructions:
        prepared["instructions"] = prepared.get("instructions", "") + instructions
        print(json.dumps({"event": "skills_loaded", "skills": selected,
                          "prompt_bytes": len(instructions.encode("utf-8"))}),
              file=sys.stderr, flush=True)
    return prepared
