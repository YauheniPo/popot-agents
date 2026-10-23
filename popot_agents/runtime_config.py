"""Load shared, non-secret runtime settings for every service image."""

import json
import math
import os
from pathlib import Path

from popot_agents.role_env import validate_role_env_names


DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "runtime.json"
FIELDS = {
    "sessions": {"default_idle_seconds", "retention_days", "prune_interval_seconds"},
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


def load_runtime_config(path: str | Path | None = None) -> dict:
    """Read and validate shared settings; changes take effect on restart."""
    config_path = Path(path or os.getenv("POPOT_RUNTIME_CONFIG", DEFAULT_PATH))
    with config_path.open(encoding="utf-8") as file:
        settings = json.load(file)
    if not isinstance(settings, dict) or set(settings) != set(FIELDS) | {"role_env_names"}:
        raise ValueError("runtime config must define sessions, timeouts, limits, logging, model, memory and role_env_names")
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
            if field == "model.temperature":
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
            elif section == "timeouts":
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
    return settings


RUNTIME = load_runtime_config()
