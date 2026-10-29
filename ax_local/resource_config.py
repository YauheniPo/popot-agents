"""Persisted, bounded resource overrides for the local monitoring services."""

import json
from pathlib import Path
import re

SERVICES = {'grafana', 'prometheus', 'kube-state-metrics', 'opentelemetry-collector'}


def cpu_cores(cpu):
    if not isinstance(cpu, str) or not re.fullmatch(r'[1-9]\d*m|[1-4]', cpu, re.ASCII):
        raise ValueError('CPU must be 25m..4 cores')
    cores = int(cpu[:-1]) / 1000 if cpu.endswith('m') else int(cpu)
    if not 0.025 <= cores <= 4:
        raise ValueError('resources exceed local bounds')
    return cores


def memory_mib(memory):
    if not isinstance(memory, str) or not re.fullmatch(r'[1-9]\d*(?:Mi|Gi)', memory, re.ASCII):
        raise ValueError('memory must use Mi or Gi')
    mib = int(memory[:-2]) * (1024 if memory.endswith('Gi') else 1)
    if not 32 <= mib <= 4096:
        raise ValueError('resources exceed local bounds')
    return mib


def resource_values(values, allow_partial):
    if not isinstance(values, dict) or set(values) - {'cpu', 'memory'} \
            or (not allow_partial and set(values) != {'cpu', 'memory'}):
        raise ValueError('resources need cpu and memory')
    parsers = {'cpu': cpu_cores, 'memory': memory_mib}
    return {name: parse(values[name]) for name, parse in parsers.items() if name in values}


def validate_resources(resources, *, allow_partial=False):
    if not isinstance(resources, dict) or set(resources) - {'requests', 'limits'} \
            or (not allow_partial and set(resources) != {'requests', 'limits'}):
        raise ValueError('resources must contain requests and limits')
    numbers = {kind: resource_values(values, allow_partial) for kind, values in resources.items()}
    for name, value in numbers.get('requests', {}).items():
        if name in numbers.get('limits', {}) and value > numbers['limits'][name]:
            raise ValueError('requests must not exceed limits')
    return resources


def load_monitoring(root: Path):
    config = json.loads((root / 'config/monitoring.json').read_text())
    if not isinstance(config, dict) or set(config) - SERVICES:
        raise ValueError('only supported monitoring services may be configured')
    for settings in config.values():
        if not isinstance(settings, dict):
            raise ValueError('monitoring service settings must be an object')
        if 'resources' in settings:
            validate_resources(settings['resources'], allow_partial=True)
    return config
