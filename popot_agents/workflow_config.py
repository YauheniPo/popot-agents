"""Shared autonomous workflow policy for API and MCP entry points."""
from popot_agents.runtime_config import RUNTIME, validate_workflow_policy


def validate_policy(policy):
    return validate_workflow_policy(policy, RUNTIME['timeouts']['max_turn_seconds'])


POLICY = RUNTIME['workflow']


def role_policy(role_config, defaults=None, *, stored=False):
    """Every role coordinates; only limits may be overridden per role."""
    if role_config is None:
        return None
    settings = role_config.get('coordination', {})
    defaults = POLICY if defaults is None else defaults
    if stored and isinstance(settings, dict):
        settings = {key: value for key, value in settings.items() if key != 'enabled'}
    overrides = set(defaults) - {'max_concurrent'}
    if not isinstance(settings, dict) or set(settings) - overrides:
        raise ValueError('coordination must contain only workflow task limits')
    return validate_policy({**defaults, **settings})
