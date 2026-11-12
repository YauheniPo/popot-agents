"""Execution errors shared by the HTTP entry point and workflow runtime."""


class AgentRunError(Exception):
    """The container did not return a valid agent answer."""
