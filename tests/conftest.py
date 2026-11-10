"""Keep the tool workspace private to each test.

``popot_agents.tools`` resolves its workspace from ``TOOL_WORKSPACE_ROOT`` and
defaults to ``/workspace``, the layout of the deployed sandbox.  A test run that
happens to contain ``/workspace/artifacts`` therefore exports the host's real
deliverables at the end of every harness run, reaching the file/subprocess layer
that most harness tests replace with mocks.  Give each test an empty root so the
suite no longer depends on the host layout.
"""

import pytest

from popot_agents import tools


@pytest.fixture(autouse=True)
def tool_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", workspace)
