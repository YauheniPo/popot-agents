"""Documentation access stays optional and does not change authenticated MCP policy."""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from popot_agents.orchestrator.main import load_agents, load_roles
from popot_agents.worker import mcp_client

ROOT = Path(__file__).resolve().parents[1]
CONTEXT7 = {'url': 'https://mcp.context7.com/mcp', 'optional': True,
            'tools': ['resolve-library-id', 'query-docs']}


def test_engineering_roles_offer_only_context7_documentation_tools():
    roles = load_roles(ROOT / 'config/roles', load_agents(ROOT / 'config/agents.json'))
    for name in ('tech_lead', 'backend_engineer', 'frontend_engineer', 'qa_engineer', 'code_reviewer'):
        assert roles[name]['mcpServers']['context7'] == CONTEXT7
    for name in ('analyst', 'chat', 'planner'):
        assert 'context7' not in roles[name].get('mcpServers', {})


def test_context7_guidance_is_only_added_to_roles_with_documentation_access():
    from tests.test_agent_reliability import AgentReliabilityTests
    helper = AgentReliabilityTests()
    for servers in ({'context7': CONTEXT7}, {}):
        with patch('popot_agents.worker.mcp_client.discover_tools', return_value={}):
            _, _, requests = helper.run_sequence([helper.result()], role={'tools': [], 'mcpServers': servers})
        instructions = '\n'.join(message['content'] for message in requests[0]['messages']
                                 if message['role'] == 'system')
        assert ('Context7' in instructions) == bool(servers)
        if servers:
            for text in ('resolve-library-id', 'query-docs', 'version', 'unavailable'):
                assert text in instructions


def test_context7_outage_is_reported_without_claiming_project_memory_failed():
    from tests.test_agent_reliability import AgentReliabilityTests
    helper = AgentReliabilityTests()
    with patch.object(mcp_client, '_discover_with_retry', new=AsyncMock(side_effect=RuntimeError('offline'))):
        _, state, requests = helper.run_sequence([helper.result()],
            role={'tools': [], 'mcpServers': {'context7': CONTEXT7}})
    assert state['outcome']['status'] == 'completed'
    assert state['outcome']['warnings'] == ['MCP server context7 is unavailable; using current sources.']
    instructions = '\n'.join(message['content'] for message in requests[0]['messages']
                             if message['role'] == 'system')
    assert 'MCP server context7 is unavailable' in instructions
    assert 'Project memory is unavailable' not in instructions


def test_public_https_transport_does_not_forward_unselected_credentials():
    seen = []
    @asynccontextmanager
    async def client(**kwargs):
        seen.append(kwargs)
        yield 'http'
    @asynccontextmanager
    async def transport(url, *, http_client):
        assert url == CONTEXT7['url']
        yield 'read', 'write'
    async def connect():
        async with mcp_client._streams(CONTEXT7) as streams:
            assert streams == ('read', 'write')
    modules = {'httpx2': SimpleNamespace(AsyncClient=client, Timeout=lambda *args, **kwargs: None),
               'mcp.client.streamable_http': SimpleNamespace(streamable_http_client=transport)}
    with patch.dict(sys.modules, modules), patch.object(mcp_client, '_bearer_token') as token:
        asyncio.run(connect())
    assert seen[0]['headers'] == {}
    assert seen[0]['follow_redirects'] is False
    token.assert_not_called()


@pytest.mark.parametrize('failure', [RuntimeError('offline'), TimeoutError()])
def test_context7_outage_preserves_required_tools_and_retries_after_cooldown(failure):
    cache = mcp_client.DiscoveryCache()
    async def discover(alias, config):
        if alias == 'context7':
            raise failure
        return {'mcp__github__read': {'server': alias}}
    with patch.object(mcp_client, '_discover_with_retry', side_effect=discover) as failed:
        result = mcp_client.discover_tools({'context7': CONTEXT7,
            'github': {'command': ['server'], 'tools': ['read']}}, cache=cache)
        assert 'mcp__github__read' in result
        assert any('context7' in warning for warning in cache.warnings)
        mcp_client.discover_tools({'context7': CONTEXT7}, cache=cache)
        assert failed.call_count == 2
    with patch.object(mcp_client, '_discover_with_retry', new=AsyncMock(return_value={
            'mcp__context7__query-docs': {'server': 'context7'}})), \
         patch.object(mcp_client.time, 'monotonic', return_value=cache.entries['context7']['retry_at'] + 1):
        assert 'mcp__context7__query-docs' in mcp_client.discover_tools({'context7': CONTEXT7}, cache=cache)
        assert cache.warnings == []


def test_public_mcp_configuration_preserves_url_and_optional_validation(tmp_path):
    agents = load_agents(ROOT / 'config/agents.json')
    role = {'agent': 'omniroute_cloud', 'instructions': 'Use docs', 'tools': [],
            'permissions': {'internet': True}, 'mcpServers': {'context7': CONTEXT7}}
    path = tmp_path / 'roles.json'
    path.write_text(json.dumps({'engineer': role}))
    assert load_roles(path, agents)['engineer']['mcpServers']['context7'] == CONTEXT7
    for changes in ({'optional': 'yes'}, {'optional': 1}, {'url': 'http://example.com'},
                    {'url': 'https://user:secret@example.com'}, {'url': 'https://example.com?key=secret'},
                    {'bearer_token_env': 'UNSELECTED_KEY'}):
        role['mcpServers']['context7'] = {**CONTEXT7, **changes}
        path.write_text(json.dumps({'engineer': role}))
        with pytest.raises(ValueError):
            load_roles(path, agents)
