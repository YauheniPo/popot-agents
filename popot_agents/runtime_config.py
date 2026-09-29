"""Load shared, non-secret runtime settings for every service image."""

import json
import os
import re
from pathlib import Path


DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "runtime.json"
FIELDS = {
    "sessions": {"default_idle_seconds", "retention_days", "prune_interval_seconds"},
    "worker": {"default_timeout_seconds", "memory", "cpus", "pids_limit", "tmpfs_mb", "workspace_tmpfs_mb",
               "startup_probe_attempts", "startup_probe_interval_seconds"},
    "timeouts": {"model_request_seconds", "mcp_discovery_seconds", "mcp_tool_seconds",
                 "mcp_upstream_seconds", "tool_command_seconds", "git_clone_seconds",
                 "file_action_seconds", "file_download_seconds", "docker_start_seconds",
                 "docker_probe_seconds", "docker_restore_seconds", "docker_inspect_seconds",
                 "docker_remove_seconds", "postgres_connect_seconds"},
    "limits": {"request_bytes", "message_chars", "history_bytes", "socket_message_bytes", "tool_output_chars",
               "write_file_bytes", "download_bytes"},
    "logging": {"mcp_body_max_bytes"},
    "model": {"default_max_tool_rounds", "temperature", "max_tokens"},
}


def validate_model_parameters(parameters: dict, label: str = "model parameters") -> dict:
    if not isinstance(parameters, dict) or set(parameters) - {"temperature", "max_tokens"}:
        raise ValueError(f"{label} may contain only temperature and max_tokens")
    temperature = parameters.get("temperature")
    if "temperature" in parameters and (type(temperature) not in (int, float)
                                        or not 0 <= temperature <= 2):
        raise ValueError(f"{label}.temperature must be between 0 and 2")
    max_tokens = parameters.get("max_tokens")
    if "max_tokens" in parameters and (type(max_tokens) is not int or max_tokens <= 0):
        raise ValueError(f"{label}.max_tokens must be positive")
    return dict(parameters)


def load_runtime_config(path: str | Path | None = None) -> dict:
    """Read and validate the complete config; changes take effect on restart."""
    config_path = Path(path or os.getenv("POPOT_RUNTIME_CONFIG", DEFAULT_PATH))
    with config_path.open(encoding="utf-8") as file:
        settings = json.load(file)
    if not isinstance(settings, dict) or set(settings) != set(FIELDS):
        raise ValueError("runtime config must define sessions, worker, timeouts, limits, logging and model")
    for section, fields in FIELDS.items():
        values = settings[section]
        if not isinstance(values, dict) or set(values) != fields:
            raise ValueError(f"runtime config has invalid {section} fields")
        for name, value in values.items():
            field = f"{section}.{name}"
            if field == "worker.memory":
                valid = isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*[kmg]", value) is not None
            elif field == "model.temperature":
                valid = value is None or (type(value) in (int, float) and 0 <= value <= 2)
            elif field == "model.max_tokens":
                valid = value is None or (type(value) is int and value > 0)
            elif field == "model.default_max_tool_rounds":
                valid = type(value) is int and 1 <= value <= 20
            elif field in {"worker.cpus", "worker.startup_probe_interval_seconds"}:
                valid = type(value) in (int, float) and value > 0
            else:
                valid = type(value) is int and value > 0
            if not valid:
                raise ValueError(f"runtime config has invalid {field}")
    if settings["limits"]["socket_message_bytes"] <= max(
            settings["limits"]["history_bytes"], settings["limits"]["request_bytes"]) + 1024:
        raise ValueError("runtime config limits.socket_message_bytes must exceed history_bytes and request_bytes")
    if settings["worker"]["default_timeout_seconds"] > 300:
        raise ValueError("runtime config worker.default_timeout_seconds must not exceed role timeout cap")
    return settings


RUNTIME = load_runtime_config()
