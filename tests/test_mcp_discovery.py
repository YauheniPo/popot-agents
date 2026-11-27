"""Discovery may retry reads, while uncertain tool calls must never replay."""

import asyncio
import importlib.util
import time
import unittest
from unittest.mock import AsyncMock, patch

from popot_agents.worker import mcp_client

REMOTE = {'url': 'https://api.githubcopilot.com/mcp/',
          'bearer_token_env': 'GITHUB_PERSONAL_ACCESS_TOKEN', 'tools': ['get_me']}


class TransportGroup(Exception):
    def __init__(self, *errors):
        super().__init__('transport task group')
        self.exceptions = errors


@unittest.skipUnless(importlib.util.find_spec('httpx2'), 'requires the installed MCP dependencies')
class DiscoveryRetryTests(unittest.TestCase):
    def setUp(self):
        import httpx2
        self.httpx = httpx2
        self.events = []
        for target, kwargs in (
                ('_bearer_token', {'return_value': 'test-secret'}),
                ('trace_event', {'side_effect': lambda event, **fields: self.events.append((event, fields))})):
            mock = patch.object(mcp_client, target, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    def test_optional_cognee_outage_does_not_hide_required_tools(self):
        async def discover(alias, config):
            if alias == 'cognee': raise RuntimeError('memory is offline')
            return {'mcp__github__get_me': {'server': alias}}
        with patch.object(mcp_client, '_discover_one', side_effect=discover):
            result = mcp_client.discover_tools({'cognee': {'command':['memory'], 'tools':[]}, 'github':REMOTE})
        self.assertIn('mcp__github__get_me', result)
        self.assertTrue(any(event=='mcp_optional_unavailable' for event, _ in self.events))

    def test_live_worker_cache_reuses_schemas_but_invalidates_changed_config(self):
        cache = mcp_client.DiscoveryCache()
        with patch.object(mcp_client, '_discover_one', new=AsyncMock(return_value={
                'mcp__github__get_me': {'server':'github'}})) as discover:
            first = mcp_client.discover_tools({'github':REMOTE}, cache=cache)
            first.clear()
            second = mcp_client.discover_tools({'github':REMOTE}, cache=cache)
            self.assertIn('mcp__github__get_me', second)
            self.assertEqual(discover.await_count, 1)
            mcp_client.discover_tools({'github':{**REMOTE,'tools':['another']}},cache=cache)
            self.assertEqual(discover.await_count, 2)

    def test_cache_retries_optional_failure_after_cooldown_and_invalidates_credentials(self):
        cache = mcp_client.DiscoveryCache()
        cognee = {'command':['memory'], 'tools':[]}
        with patch.object(mcp_client, '_discover_one', new=AsyncMock(side_effect=[
                RuntimeError('offline'), {}])) as discover, \
             patch.object(mcp_client.time, 'monotonic', return_value=0) as clock:
            mcp_client.discover_tools({'cognee':cognee}, cache=cache)
            mcp_client.discover_tools({'cognee':cognee}, cache=cache)
            self.assertEqual(discover.await_count, 1)
            self.assertTrue(cache.warnings)
            clock.return_value = 61
            mcp_client.discover_tools({'cognee':cognee}, cache=cache)
            self.assertEqual(discover.await_count, 2)
            self.assertEqual(cache.warnings, [])
        with patch.object(mcp_client, '_discover_one', new=AsyncMock(return_value={})) as discover:
            mcp_client.discover_tools({'github':REMOTE}, cache=cache)
            with patch.object(mcp_client, '_bearer_token', return_value='rotated-secret'):
                mcp_client.discover_tools({'github':REMOTE}, cache=cache)
            self.assertEqual(discover.await_count, 2)
            self.assertNotIn('rotated-secret', str(cache.entries))

    def test_nested_connect_timeout_recovers_and_reports_progress_without_token(self):
        failure = TransportGroup(TransportGroup(self.httpx.ConnectTimeout('test-secret')))
        with patch.object(mcp_client, '_discover_one', new=AsyncMock(
                side_effect=[failure, {'mcp__github__get_me': {}}])) as discover, \
                patch.object(mcp_client.asyncio, 'sleep', new=AsyncMock()) as sleep:
            result = mcp_client.discover_tools({'github': REMOTE}, timeout_seconds=10)
        self.assertEqual(set(result), {'mcp__github__get_me'})
        self.assertEqual(discover.await_count, 2)
        sleep.assert_awaited_once_with(1)
        self.assertIn('mcp_discovery_retry', [event for event, _ in self.events])
        self.assertEqual(self.events[-1][0], 'mcp_discovery_completed')
        self.assertNotIn('test-secret', str(self.events))

    def test_permanent_network_failure_stops_after_three_attempts(self):
        with patch.object(mcp_client, '_discover_one', new=AsyncMock(
                side_effect=self.httpx.ConnectError('test-secret'))) as discover, \
                patch.object(mcp_client.asyncio, 'sleep', new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(RuntimeError, 'github.*ConnectError') as raised:
                mcp_client.discover_tools({'github': REMOTE})
        self.assertEqual(discover.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertNotIn('test-secret', str(raised.exception))
        self.assertEqual(self.events[-1][0], 'mcp_discovery_failed')

    def test_config_controls_attempt_limit_and_linear_backoff(self):
        for attempts in (1, 2, 4):
            with self.subTest(attempts=attempts), patch.dict(mcp_client.RUNTIME, {
                    'mcp': {'discovery_attempts': attempts, 'discovery_backoff_seconds': .25}}), \
                    patch.object(mcp_client, '_discover_one', new=AsyncMock(
                        side_effect=self.httpx.ConnectError('unavailable'))) as discover, \
                    patch.object(mcp_client.asyncio, 'sleep', new=AsyncMock()) as sleep:
                with self.assertRaisesRegex(RuntimeError, 'ConnectError'):
                    mcp_client.discover_tools({'github': REMOTE})
                self.assertEqual(discover.await_count, attempts)
                self.assertEqual([call.args[0] for call in sleep.await_args_list],
                                 [.25 * attempt for attempt in range(1, attempts)])

    def test_nontransient_errors_and_stdio_failures_are_not_retried(self):
        request = self.httpx.Request('POST', REMOTE['url'])
        errors = [ValueError('missing token'), RuntimeError('missing tool'),
                  self.httpx.UnsupportedProtocol('bad URL')]
        for status in (401, 403, 404):
            errors.append(self.httpx.HTTPStatusError('denied', request=request,
                          response=self.httpx.Response(status, request=request)))
        errors.append(TransportGroup(self.httpx.ConnectTimeout('connect stalled'), errors[0]))
        for config, error in [(REMOTE, e) for e in errors] + [
                ({'command': ['server'], 'tools': []}, self.httpx.ConnectTimeout('connect stalled'))]:
            with self.subTest(error=error, config=config), patch.object(
                    mcp_client, '_discover_one', new=AsyncMock(side_effect=error)) as discover, \
                    patch.object(mcp_client.asyncio, 'sleep', new=AsyncMock()) as sleep:
                with self.assertRaises(RuntimeError):
                    mcp_client.discover_tools({'github': config})
                self.assertEqual(discover.await_count, 1)
                sleep.assert_not_awaited()

    def test_backoff_shares_deadline_and_does_not_start_next_server(self):
        with patch.object(mcp_client, '_discover_one', new=AsyncMock(
                side_effect=self.httpx.ConnectTimeout('connect stalled'))) as discover:
            start = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, 'timed out during discovery'):
                mcp_client.discover_tools({'github': REMOTE, 'second': REMOTE}, timeout_seconds=.03)
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(discover.await_count, 1)

    def test_deadline_cancels_and_cleans_up_second_attempt(self):
        closed = []
        async def slow(*args):
            try:
                await asyncio.Event().wait()
            finally:
                closed.append(True)
        count = 0
        async def discover(*args):
            nonlocal count
            count += 1
            if count == 1:
                raise self.httpx.ConnectTimeout('connect stalled')
            return await slow()
        with patch.object(mcp_client, '_discover_one', side_effect=discover), \
                patch.object(mcp_client.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'timed out during discovery'):
                mcp_client.discover_tools({'github': REMOTE}, timeout_seconds=.03)
        self.assertEqual(count, 2)
        self.assertEqual(closed, [True])

    def test_tool_transport_failure_is_never_automatically_replayed(self):
        with patch.object(mcp_client, '_call_one', new=AsyncMock(
                side_effect=self.httpx.ConnectTimeout('connect stalled'))) as call:
            with self.assertRaises(mcp_client.MCPTransportError):
                mcp_client.call_tool(REMOTE, 'submit_review', {})
        self.assertEqual(call.await_count, 1)
