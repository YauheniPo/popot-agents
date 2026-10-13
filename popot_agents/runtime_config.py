"""Load shared, non-secret runtime settings for every service image."""

import json
import math
import os
import re
from pathlib import Path

from popot_agents.role_env import validate_role_env_names


DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "runtime.json"
FIELDS = {
    "sessions": {"default_idle_seconds", "retention_days", "prune_interval_seconds"},
    "worker": {"default_timeout_seconds", "max_concurrent_tasks", "memory", "cpus", "pids_limit", "tmpfs_mb", "workspace_tmpfs_mb",
               "startup_probe_attempts", "startup_probe_interval_seconds",
               "max_timeout_seconds", "min_timeout_seconds", "response_margin_seconds"},
    "timeouts": {"model_request_seconds", "mcp_discovery_seconds", "mcp_tool_seconds",
                 "mcp_upstream_seconds", "tool_command_seconds", "git_clone_seconds",
                 "file_action_seconds", "file_download_seconds", "docker_start_seconds",
                 "docker_probe_seconds", "docker_restore_seconds", "docker_inspect_seconds",
                 "docker_remove_seconds", "postgres_connect_seconds",
                 "memory_request_seconds", "memory_recall_seconds", "memory_session_start_seconds",
                 "memory_session_finish_seconds", "memory_summary_seconds",
                 "memory_capture_min_seconds", "memory_capture_margin_seconds", "memory_recall_min_seconds", "memory_tool_request_seconds",
                 "memory_skill_import_seconds"},
    "limits": {"request_bytes", "message_chars", "history_bytes", "socket_message_bytes", "tool_output_chars",
               "write_file_bytes", "download_bytes", "skill_prompt_bytes"},
    "logging": {"mcp_body_max_bytes", "worker_log_tail_lines", "worker_log_max_bytes"},
    "model": {"default_max_tool_rounds", "request_retries", "temperature", "max_tokens", "final_answer_reserve_seconds", "final_answer_reserve_divisor", "max_tool_rounds"},
    "memory": {"summary_attempts", "summary_max_tokens", "summary_retry_max_tokens", "recall_top_k",
               "task_chars", "query_chars", "chunk_chars", "context_chars", "tool_evidence_chars",
               "tool_evidence_count", "answer_chars", "max_memories", "text_chars", "quote_chars",
               "trace_tool_limit"},
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
    if not isinstance(settings, dict) or set(settings) != set(FIELDS) | {"role_env_names"}:
        raise ValueError("runtime config must define sessions, worker, timeouts, limits, logging, model, memory and role_env_names")
    try:
        validate_role_env_names(settings["role_env_names"])
    except ValueError as exc:
        raise ValueError("runtime config has invalid role_env_names") from exc
    for section, fields in FIELDS.items():
        values = settings[section]
        if section == "model" and isinstance(values, dict):
            values.setdefault("request_retries", 1)
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
                cap = settings["model"]["max_tool_rounds"]
                valid = type(value) is int and type(cap) is int and 1 <= value <= cap
            elif field == "model.request_retries":
                if type(value) is not int or value < 0:
                    raise ValueError("runtime config model.request_retries must be a non-negative integer "
                                     "(0 disables retries)")
                valid = True
            elif (section == "timeouts" or field in {"worker.cpus", "worker.startup_probe_interval_seconds",
                                                     "worker.min_timeout_seconds"}):
                valid = type(value) in (int, float) and math.isfinite(value) and value > 0
            else:
                valid = type(value) is int and value > 0
            if not valid:
                raise ValueError(f"runtime config has invalid {field}")
    if settings["limits"]["socket_message_bytes"] <= max(
            settings["limits"]["history_bytes"], settings["limits"]["request_bytes"]) + 1024:
        raise ValueError("runtime config limits.socket_message_bytes must exceed history_bytes and request_bytes")
    if settings["worker"]["default_timeout_seconds"] > settings["worker"]["max_timeout_seconds"]:
        raise ValueError("runtime config worker.default_timeout_seconds must not exceed role timeout cap")
    if settings['timeouts']['memory_capture_min_seconds'] <= settings['timeouts']['memory_capture_margin_seconds']:
        raise ValueError('runtime config memory_capture_min_seconds must exceed memory_capture_margin_seconds')
    if settings['worker']['min_timeout_seconds'] >= settings['worker']['default_timeout_seconds']:
        raise ValueError('runtime config worker.min_timeout_seconds must be below default_timeout_seconds')
    if not 1 <= settings['memory']['max_memories'] <= 5 or settings['memory']['trace_tool_limit'] > 200:
        raise ValueError('runtime config memory limits exceed the Cognee session API contract')
    if settings['memory']['recall_top_k'] > 20:
        raise ValueError('runtime config memory.recall_top_k must not exceed 20')
    if settings['worker']['response_margin_seconds'] < (2 * settings['timeouts']['memory_session_start_seconds']
                                                        + settings['timeouts']['memory_session_finish_seconds']):
        raise ValueError('runtime config worker.response_margin_seconds must cover memory session requests')
    return settings


RUNTIME = load_runtime_config()
