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
    "docker": {"max_concurrent_tasks", "memory", "cpus", "pids_limit", "tmpfs_mb", "workspace_tmpfs_mb",
               "startup_probe_attempts", "startup_probe_interval_seconds", "start_timeout_seconds",
               "probe_timeout_seconds", "restore_timeout_seconds", "inspect_timeout_seconds", "remove_timeout_seconds"},
    "timeouts": {"model_request_seconds", "mcp_discovery_seconds", "mcp_tool_seconds",
                 "mcp_upstream_seconds", "tool_command_seconds", "git_clone_seconds",
                 "file_action_seconds", "file_download_seconds", "postgres_connect_seconds",
                 "turn_seconds", "max_turn_seconds", "min_turn_seconds", "turn_response_margin_seconds",
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
    if not isinstance(parameters, dict) or set(parameters) - {
            "temperature", "max_tokens", "reasoning", "chat_template_kwargs"}:
        raise ValueError(f"{label} may contain only temperature, max_tokens, reasoning and chat_template_kwargs")
    temperature = parameters.get("temperature")
    if "temperature" in parameters and (type(temperature) not in (int, float)
                                        or not 0 <= temperature <= 2):
        raise ValueError(f"{label}.temperature must be between 0 and 2")
    max_tokens = parameters.get("max_tokens")
    if "max_tokens" in parameters and (type(max_tokens) is not int or max_tokens <= 0):
        raise ValueError(f"{label}.max_tokens must be positive")
    result = dict(parameters)
    if 'reasoning' in parameters:
        reasoning = parameters['reasoning']
        if not isinstance(reasoning, dict) or set(reasoning) - {'effort', 'max_tokens', 'enabled', 'exclude'}:
            raise ValueError(f'{label}.reasoning must be an object with effort, max_tokens, enabled or exclude')
        if 'effort' in reasoning and 'max_tokens' in reasoning:
            raise ValueError(f'{label}.reasoning must use either effort or max_tokens, not both')
        if 'effort' in reasoning and (not isinstance(reasoning['effort'], str) or
                reasoning['effort'] not in {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}):
            raise ValueError(f'{label}.reasoning.effort must be none, minimal, low, medium, high, xhigh or max')
        if 'max_tokens' in reasoning and (type(reasoning['max_tokens']) is not int or reasoning['max_tokens'] <= 0):
            raise ValueError(f'{label}.reasoning.max_tokens must be positive')
        for name in ('enabled', 'exclude'):
            if name in reasoning and type(reasoning[name]) is not bool:
                raise ValueError(f'{label}.reasoning.{name} must be a boolean')
        result['reasoning'] = dict(reasoning)
    if 'chat_template_kwargs' in parameters:
        options = parameters['chat_template_kwargs']
        if not isinstance(options, dict) or set(options) - {'enable_thinking'}:
            raise ValueError(f'{label}.chat_template_kwargs must be an object with enable_thinking')
        if 'enable_thinking' in options and type(options['enable_thinking']) is not bool:
            raise ValueError(f'{label}.chat_template_kwargs.enable_thinking must be a boolean')
        result['chat_template_kwargs'] = dict(options)
    return result


def _validate_ax_config(settings: dict, timeouts: dict) -> None:
    ax = settings
    if not isinstance(ax, dict) or set(ax) != {
            "delegation", "proxy",
            "context", "atespace", "workspace_name", "task_resources", "task_debug",
            "cli_timeout_seconds", "route_timeout_seconds", "startup_timeout_seconds",
            "startup_probe_interval_seconds", "session_backend", "probe_timeout_seconds",
            "restore_timeout_seconds", "response_margin_seconds", "delete_timeout_seconds",
            "suspend_retention_seconds", "max_workers", "worker_queue_timeout_seconds", "worker_pool_name"}:
        raise ValueError("runtime config has invalid ax settings")
    if not isinstance(ax["context"], str) or not re.fullmatch(r"kind-[a-z0-9-]+", ax["context"]):
        raise ValueError("AX config ax.context must name a local kind context")
    if not isinstance(ax['worker_pool_name'], str) or not re.fullmatch(
            r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', ax['worker_pool_name']):
        raise ValueError('AX config ax.worker_pool_name must be a Kubernetes DNS label')
    if not all(isinstance(ax[key], str) and re.fullmatch(r"[a-z0-9-]+", ax[key])
               for key in ("atespace", "workspace_name")):
        raise ValueError("AX config has invalid atespace or workspace name")
    resources = ax["task_resources"]
    if not isinstance(resources, dict) or set(resources) != {"requests", "limits"} or any(
            not isinstance(resources[key], dict) or set(resources[key]) != {"cpu", "memory"}
            or not all(isinstance(value, str) and value for value in resources[key].values())
            for key in ("requests", "limits")):
        raise ValueError("AX config has invalid task resources")
    if type(ax["task_debug"]) is not bool:
        raise ValueError("AX config ax.task_debug must be boolean")
    for key in ("cli_timeout_seconds", "route_timeout_seconds", "startup_timeout_seconds",
                "startup_probe_interval_seconds", "probe_timeout_seconds",
                "restore_timeout_seconds", "response_margin_seconds", "delete_timeout_seconds",
                "suspend_retention_seconds", "max_workers", "worker_queue_timeout_seconds"):
        if type(ax[key]) is not int or ax[key] <= 0:
            raise ValueError(f"AX config ax.{key} must be positive")
    if ax["route_timeout_seconds"] > 3600:
        raise ValueError("AX config ax.route_timeout_seconds must not exceed 3600")
    if ax['route_timeout_seconds'] <= timeouts['max_turn_seconds'] + ax['response_margin_seconds']:
        raise ValueError('AX config ax.route_timeout_seconds must exceed shared worker max timeout and response margin')
    if ax["session_backend"] not in ("postgres", "file"):
        raise ValueError("AX config ax.session_backend must be postgres or file")
    proxy = settings["proxy"]
    if not isinstance(proxy, dict) or set(proxy) != {
            "port", "max_request_bytes", "max_response_bytes", "response_margin_seconds"}:
        raise ValueError("AX config has invalid proxy settings")
    if type(proxy["port"]) is not int or not 1 <= proxy["port"] <= 65535:
        raise ValueError("AX config proxy.port must be a TCP port")
    for key in ("max_request_bytes", "max_response_bytes", "response_margin_seconds"):
        if type(proxy[key]) is not int or proxy[key] <= 0:
            raise ValueError(f"AX config proxy.{key} must be positive")
    delegation = settings["delegation"]
    if not isinstance(delegation, dict) or set(delegation) != {
            "enabled", "port", "max_depth", "max_calls_per_turn",
            "turn_timeout_seconds", "poll_interval_seconds"}:
        raise ValueError("AX config has invalid delegation settings")
    if type(delegation["enabled"]) is not bool:
        raise ValueError("AX config delegation.enabled must be boolean")
    for key, maximum in {"port": 65535, "max_depth": 8, "max_calls_per_turn": 32,
                         "turn_timeout_seconds": timeouts["max_turn_seconds"]}.items():
        if type(delegation[key]) is not int or not 1 <= delegation[key] <= maximum:
            raise ValueError(f"AX config delegation.{key} is out of range")
    interval = delegation["poll_interval_seconds"]
    if type(interval) not in {int, float} or not 0.05 <= interval <= 5:
        raise ValueError("AX config delegation.poll_interval_seconds is out of range")
    if delegation["port"] == proxy["port"]:
        raise ValueError("AX delegation and proxy ports must differ")


def load_runtime_config(path: str | Path | None = None) -> dict:
    """Read and validate the complete config; changes take effect on restart."""
    config_path = Path(path or os.getenv("POPOT_RUNTIME_CONFIG", DEFAULT_PATH))
    with config_path.open(encoding="utf-8") as file:
        settings = json.load(file)
    if not isinstance(settings, dict) or set(settings) != set(FIELDS) | {"role_env_names", "ax"}:
        raise ValueError("runtime config must define sessions, docker, ax, timeouts, limits, logging, model, memory and role_env_names")
    try:
        validate_role_env_names(settings["role_env_names"])
    except ValueError as exc:
        raise ValueError("runtime config has invalid role_env_names") from exc
    for section, fields in FIELDS.items():
        values = settings[section]
        if not isinstance(values, dict) or set(values) != fields:
            raise ValueError(f"runtime config has invalid {section} fields")
        for name, value in values.items():
            field = f"{section}.{name}"
            if field == "docker.memory":
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
            elif field in {"timeouts.turn_seconds", "timeouts.max_turn_seconds", "timeouts.turn_response_margin_seconds"}:
                valid = type(value) is int and value > 0
            elif (section == "timeouts" or field in {"docker.cpus", "docker.startup_probe_interval_seconds"}
                  or (section == "docker" and name.endswith("_timeout_seconds"))):
                valid = type(value) in (int, float) and math.isfinite(value) and value > 0
            else:
                valid = type(value) is int and value > 0
            if not valid:
                raise ValueError(f"runtime config has invalid {field}")
    if settings["limits"]["socket_message_bytes"] <= max(
            settings["limits"]["history_bytes"], settings["limits"]["request_bytes"]) + 1024:
        raise ValueError("runtime config limits.socket_message_bytes must exceed history_bytes and request_bytes")
    if settings["timeouts"]["turn_seconds"] > settings["timeouts"]["max_turn_seconds"]:
        raise ValueError("runtime config timeouts.turn_seconds must not exceed role timeout cap")
    if settings['timeouts']['memory_capture_min_seconds'] <= settings['timeouts']['memory_capture_margin_seconds']:
        raise ValueError('runtime config memory_capture_min_seconds must exceed memory_capture_margin_seconds')
    if settings['timeouts']['min_turn_seconds'] >= settings['timeouts']['turn_seconds']:
        raise ValueError('runtime config timeouts.min_turn_seconds must be below turn_seconds')
    if not 1 <= settings['memory']['max_memories'] <= 5 or settings['memory']['trace_tool_limit'] > 200:
        raise ValueError('runtime config memory limits exceed the Cognee session API contract')
    if settings['memory']['recall_top_k'] > 20:
        raise ValueError('runtime config memory.recall_top_k must not exceed 20')
    if settings['timeouts']['turn_response_margin_seconds'] < (2 * settings['timeouts']['memory_session_start_seconds']
                                                        + settings['timeouts']['memory_session_finish_seconds']):
        raise ValueError('runtime config timeouts.turn_response_margin_seconds must cover memory session requests')
    _validate_ax_config(settings["ax"], settings["timeouts"])
    return settings


RUNTIME = load_runtime_config()
AX_CONFIG = RUNTIME["ax"]
