"""Load AX-specific, non-secret runtime settings."""

from __future__ import annotations

import json
import re
from pathlib import Path


DEFAULT_PATH = Path(__file__).with_name("config.json")


def load_ax_config(path: str | Path | None = None) -> dict:
    config_path = Path(path or DEFAULT_PATH)
    with config_path.open(encoding="utf-8") as source:
        settings = json.load(source)
    if not isinstance(settings, dict) or set(settings) != {
            "default_provider", "default_model", "ax", "proxy", "delegation"}:
        raise ValueError("AX config must define provider, model, ax, proxy and delegation settings")
    if not all(isinstance(settings[key], str) and settings[key].strip()
               for key in ("default_provider", "default_model")):
        raise ValueError("AX default provider and model must be nonempty strings")
    ax = settings["ax"]
    if not isinstance(ax, dict) or set(ax) != {
            "context", "atespace", "workspace_name", "task_resources", "task_debug",
            "cli_timeout_seconds", "route_timeout_seconds", "startup_timeout_seconds",
            "startup_probe_interval_seconds", "session_backend"}:
        raise ValueError("AX config has invalid ax settings")
    if not isinstance(ax["context"], str) or not re.fullmatch(r"kind-[a-z0-9-]+", ax["context"]):
        raise ValueError("AX config ax.context must name a local kind context")
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
                "startup_probe_interval_seconds"):
        if type(ax[key]) is not int or ax[key] <= 0:
            raise ValueError(f"AX config ax.{key} must be positive")
    if ax["route_timeout_seconds"] > 3600:
        raise ValueError("AX config ax.route_timeout_seconds must not exceed 3600")
    if ax["session_backend"] not in ("postgres", "file"):
        raise ValueError("AX config ax.session_backend must be postgres or file")
    proxy = settings["proxy"]
    if isinstance(proxy, dict):
        proxy.setdefault("response_margin_seconds", 5)
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
            "enabled", "port", "max_depth", "max_calls_per_turn", "max_concurrent_tasks",
            "turn_timeout_seconds", "poll_interval_seconds"}:
        raise ValueError("AX config has invalid delegation settings")
    if type(delegation["enabled"]) is not bool:
        raise ValueError("AX config delegation.enabled must be boolean")
    for key, maximum in {"port": 65535, "max_depth": 8, "max_calls_per_turn": 32,
                         "max_concurrent_tasks": 16, "turn_timeout_seconds": 300}.items():
        if type(delegation[key]) is not int or not 1 <= delegation[key] <= maximum:
            raise ValueError(f"AX config delegation.{key} is out of range")
    interval = delegation["poll_interval_seconds"]
    if type(interval) not in {int, float} or not 0.05 <= interval <= 5:
        raise ValueError("AX config delegation.poll_interval_seconds is out of range")
    if delegation["port"] == proxy["port"]:
        raise ValueError("AX delegation and proxy ports must differ")
    return settings


AX_CONFIG = load_ax_config()
