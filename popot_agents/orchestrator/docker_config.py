"""Load Docker execution settings only when this backend is used."""

import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path


DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config.json"
FIELDS = {"max_concurrent_tasks", "memory", "cpus", "pids_limit", "tmpfs_mb",
          "workspace_tmpfs_mb", "startup_probe_attempts", "startup_probe_interval_seconds",
          "start_timeout_seconds", "probe_timeout_seconds", "restore_timeout_seconds",
          "inspect_timeout_seconds", "remove_timeout_seconds"}


def load_docker_config(path: str | Path | None = None) -> dict:
    config_path = Path(path or os.getenv("POPOT_DOCKER_CONFIG", DEFAULT_PATH))
    with config_path.open(encoding="utf-8") as file:
        settings = json.load(file)
    if not isinstance(settings, dict) or set(settings) != FIELDS:
        raise ValueError("Docker config has invalid fields")
    for name, value in settings.items():
        if name == "memory":
            valid = isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*[kmg]", value) is not None
        elif name in {"cpus", "startup_probe_interval_seconds"} or name.endswith("_timeout_seconds"):
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        else:
            valid = type(value) is int and value > 0
        if not valid:
            raise ValueError(f"Docker config has invalid {name}")
    return settings


@lru_cache(maxsize=1)
def get_docker_config() -> dict:
    # AX reuses the HTTP server and profile validation, without Docker execution.
    return load_docker_config()
