"""Load settings specific to the local AX backend."""

import json
import os
import re
from pathlib import Path

from popot_agents.runtime_config import RUNTIME


DEFAULT_PATH = Path(__file__).with_name("config.json")


def _validate_ax_config(settings: dict, timeouts: dict) -> None:
    ax = settings
    if not isinstance(ax, dict) or set(ax) != {
            "delegation", "proxy",
            "context", "atespace", "workspace_name", "task_resources", "task_debug",
            "cli_timeout_seconds", "route_timeout_seconds", "startup_timeout_seconds",
            "startup_probe_interval_seconds", "session_backend", "probe_timeout_seconds",
            "restore_timeout_seconds", "response_margin_seconds", "delete_timeout_seconds",
            "suspend_retention_seconds", "snapshot_mode", "max_workers", "worker_queue_timeout_seconds", "worker_pool_name"}:
        raise ValueError("AX config has invalid ax settings")
    if not isinstance(ax["context"], str) or not re.fullmatch(r"kind-[a-z0-9-]+", ax["context"]):
        raise ValueError("AX config context must name a local kind context")
    if not isinstance(ax['worker_pool_name'], str) or not re.fullmatch(
            r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', ax['worker_pool_name']):
        raise ValueError('AX config worker_pool_name must be a Kubernetes DNS label')
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
        raise ValueError("AX config task_debug must be boolean")
    if ax['snapshot_mode'] not in ('data', 'full'):
        raise ValueError('AX config snapshot_mode must be data or full')
    for key in ("cli_timeout_seconds", "route_timeout_seconds", "startup_timeout_seconds",
                "startup_probe_interval_seconds", "probe_timeout_seconds",
                "restore_timeout_seconds", "response_margin_seconds", "delete_timeout_seconds",
                "suspend_retention_seconds", "max_workers", "worker_queue_timeout_seconds"):
        if type(ax[key]) is not int or ax[key] <= 0:
            raise ValueError(f"AX config {key} must be positive")
    if ax["route_timeout_seconds"] > 3600:
        raise ValueError("AX config route_timeout_seconds must not exceed 3600")
    if ax['route_timeout_seconds'] <= timeouts['max_turn_seconds'] + ax['response_margin_seconds']:
        raise ValueError('AX config route_timeout_seconds must exceed shared worker max timeout and response margin')
    if ax["session_backend"] not in ("postgres", "file"):
        raise ValueError("AX config session_backend must be postgres or file")
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


def load_ax_config(path: str | Path | None = None, *, runtime: dict | None = None) -> dict:
    """Validate AX settings against the shared runtime's turn budgets."""
    config_path = Path(path or os.getenv("POPOT_AX_CONFIG", DEFAULT_PATH))
    with config_path.open(encoding="utf-8") as file:
        settings = json.load(file)
    _validate_ax_config(settings, (RUNTIME if runtime is None else runtime)["timeouts"])
    return settings


AX_CONFIG = load_ax_config()
