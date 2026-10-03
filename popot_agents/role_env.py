"""Explicit per-role environment forwarding, without persisting secret values."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RESERVED_PREFIXES = ("HARNESS_", "AX_", "POPOT_", "TOOL_", "XDG_", "PYTHON", "LD_", "DYLD_")
_RESERVED_NAMES = {"HOME", "PATH", "ENV", "IFS", "BASH_ENV", "SHELLOPTS",
                   "GIT_TERMINAL_PROMPT", "SSL_CERT_FILE", "SSL_CERT_DIR"}


def validate_role_env_names(names) -> list[str]:
    if not isinstance(names, list) or any(
            not isinstance(name, str) or not _NAME.fullmatch(name)
            or name in _RESERVED_NAMES or name.startswith(_RESERVED_PREFIXES)
            for name in names) or len(names) != len(set(names)):
        raise ValueError("role env_names must list distinct, non-reserved variable names")
    return names


def selected_role_env(names, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    selected = {}
    for name in validate_role_env_names(names):
        value = environ.get(name)
        if not value:
            raise ValueError(f"required role environment variable is missing: {name}")
        selected[name] = value
    return selected
