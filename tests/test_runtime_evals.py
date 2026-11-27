"""Runtime baselines grade observations, not an agent's claims of success."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


def span(kind='LLM', sid='1', **attributes):
    return {'context': {'span_id': sid, 'trace_id': 'trace'}, 'name': kind,
            'span_kind': kind, 'parent_id': None, 'status_code': 'OK',
            'start_time': '2026-11-12T10:00:00+00:00',
            'end_time': '2026-11-12T10:00:02+00:00',
            'attributes': {'session.id': 'root', 'popot.root_session_id': 'root',
                           'popot.agent_session_id': 'root', 'popot.role': 'planner',
                           **attributes}, 'events': []}


class RuntimeEvalTests(unittest.TestCase):
    def test_missing_usage_is_unknown_and_never_a_zero_token_total(self):
        from evals.runtime import build_report, evaluate_span
        known = span(**{'llm.token_count.total': 10, 'llm.token_count.prompt': 7,
                        'llm.token_count.completion': 3, 'llm.model_name': 'actual'})
        missing = span(sid='2')
        report = build_report([known, missing], {})
        self.assertEqual(report['summary']['tokens_known'], 10)
        self.assertIsNone(report['summary']['tokens'])
        self.assertEqual(report['summary']['llm_usage_coverage'], .5)
        self.assertEqual(evaluate_span(missing)['harness.usage_complete'], 0)
        self.assertNotIn('harness.tokens_total', evaluate_span(missing))

    def test_declared_completed_is_not_functional_success(self):
        from evals.runtime import build_report, evaluate_span
        completed = span('AGENT', **{'popot.outcome': 'completed'})
        completed['events'] = [{'name': 'final_result_invalid'}] * 64
        metrics = evaluate_span(completed)
        self.assertEqual(metrics['harness.declared_completed'], 1)
        self.assertNotIn('harness.functional_success', metrics)
        report = build_report([completed], {})
        self.assertIsNone(report['summary']['functional_success_rate'])
        self.assertIsNone(report['summary']['false_completion_rate'])
        self.assertEqual(report['audit']['event_tail_at_limit'], 1)
        self.assertEqual(report['summary']['retained_event_counts']['final_result_invalid'], 64)

    def test_legacy_agent_identity_and_broken_phoenix_grouping_stay_visible(self):
        from evals.runtime import build_report, evaluate_span
        old = span('AGENT', **{'session.id': 'child', 'metadata': json.dumps({'session_id': 'child'})})
        del old['attributes']['popot.agent_session_id']
        old['attributes']['popot.parent_session_id'] = 'root'
        report = build_report([old], {})
        self.assertEqual(evaluate_span(old)['harness.session_link_ok'], 0)
        self.assertEqual(report['audit']['missing_agent_session_id'], 1)
        self.assertEqual(report['audit']['legacy_agent_identity'], 1)
        self.assertEqual(report['summary']['root_sessions'], 1)
        self.assertEqual(report['sessions'][0]['agent_sessions'], ['child'])

    def test_statuses_negative_duration_and_partial_window_are_not_guessed(self):
        from evals.runtime import build_report, evaluate_span
        tool = span('TOOL', **{'popot.incomplete': True})
        tool['status_code'] = 'ERROR'
        tool['parent_id'] = 'outside-window'
        self.assertEqual(evaluate_span(tool)['harness.runtime_ok'], 0)
        self.assertEqual(evaluate_span(tool)['harness.tool_finished'], 0)
        tool['status_code'] = 'UNSET'
        tool['end_time'] = '2026-11-12T09:59:59+00:00'
        self.assertNotIn('harness.runtime_ok', evaluate_span(tool))
        self.assertNotIn('harness.duration_seconds', evaluate_span(tool))
        report = build_report([tool], {})
        self.assertEqual(report['audit']['parent_outside_snapshot'], 1)
        self.assertEqual(report['audit']['invalid_duration'], 1)
        self.assertFalse(report['sessions'][0]['root_agent_observed'])
        self.assertIsNone(build_report([], {})['summary']['runtime_error_rate'])

    def test_latency_groups_and_usage_do_not_double_count_agent_time(self):
        from evals.runtime import build_report
        llm = span(**{'llm.model_name': 'actual', 'llm.token_count.total': 10,
                      'llm.token_count.prompt': 7, 'llm.token_count.completion': 3})
        agent = span('AGENT', '2', **{'popot.outcome': 'completed'})
        agent['end_time'] = '2026-11-12T10:00:10+00:00'
        report = build_report([llm, agent], {})
        self.assertEqual(report['by_kind']['LLM']['latency_seconds']['p95'], 2)
        self.assertEqual(report['by_model']['actual']['tokens'], 10)
        self.assertEqual(report['sessions'][0]['observed_wall_seconds'], 10)
        self.assertEqual(report['summary']['tokens'], 10)

    def test_only_metadata_allowlist_is_saved(self):
        from evals.runtime import sanitize_span
        raw = span(**{'input.value': 'private task', 'output.value': 'private answer',
                      'tool.parameters': 'secret args', 'metadata': json.dumps({
                          'session_id': 'root', 'password': 'secret'}), 'authorization': 'Bearer secret'})
        raw['events'] = [{'name': 'work_evidence', 'attributes': {'output': 'secret'}}]
        raw['status_message'] = 'private failure'
        cleaned = sanitize_span(raw)
        self.assertNotIn('secret', json.dumps(cleaned))
        self.assertNotIn('private', json.dumps(cleaned))
        self.assertEqual(cleaned['events'], [{'name': 'work_evidence'}])
        self.assertEqual(cleaned['attributes']['metadata'], {'session_id': 'root'})

    def test_oversize_phoenix_response_is_rejected_before_json_decoding(self):
        import io
        from evals.runtime import API
        api = API('http://localhost:6006', 'fixture')
        with patch('evals.runtime.MAX_RESPONSE_BYTES', 4), patch.object(
                api.opener, 'open', return_value=io.BytesIO(b'00000')), self.assertRaisesRegex(ValueError, 'size limit'):
            api.call('/v1/projects')

    def test_capture_paginates_with_same_window_and_rejects_repeated_cursor(self):
        from evals.runtime import fetch_spans
        api = Mock()
        api.call.side_effect = [{'data': [span()], 'next_cursor': 'next'},
                                {'data': [span(sid='2')], 'next_cursor': None}]
        rows = fetch_spans(api, 'project', 'start', 'end')
        self.assertEqual(len(rows), 2)
        first, second = [c.kwargs['params'] for c in api.call.call_args_list]
        self.assertEqual(first['start_time'], second['start_time'])
        self.assertEqual(second['end_time'], 'end')
        self.assertEqual(second['cursor'], 'next')
        self.assertNotIn('parent_id', first)
        api.call.side_effect = None
        api.call.return_value = {'data': [span()], 'next_cursor': 'repeat'}
        with self.assertRaisesRegex(ValueError, 'cursor'):
            fetch_spans(api, 'project', 'start', 'end')

    def test_annotation_retry_uses_stable_identity_and_skips_unknown_scores(self):
        from evals.runtime import annotate
        api = Mock()
        api.call.side_effect = lambda *a, **kw: {'data': [{'id': str(i)} for i, _ in enumerate(kw['payload']['data'])]}
        source = span()
        original = deepcopy(source)
        first = annotate(api, [source])
        payload = api.call.call_args.kwargs['payload']
        second = annotate(api, [source])
        self.assertEqual(first, second)
        self.assertEqual(payload, api.call.call_args.kwargs['payload'])
        self.assertEqual(source, original)
        self.assertTrue(all(a['annotator_kind'] == 'CODE' and a['identifier'] == 'harness-baseline-v1'
                            for a in payload['data']))
        self.assertFalse(any(a['name'] == 'harness.tokens_total' for a in payload['data']))

    def test_http_client_does_not_forward_credentials_through_redirects_or_error_bodies(self):
        from evals.runtime import API
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302 if self.path == '/redirect' else 403)
                self.send_header('Location', '/credential-leak')
                self.end_headers()
                self.wfile.write(b'Bearer private-key')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = API(f'http://127.0.0.1:{server.server_port}', 'private-key')
            for path, code in [('/redirect', 302), ('/forbidden', 403)]:
                with self.assertRaisesRegex(RuntimeError, f'^Phoenix HTTP {code}$'):
                    api.call(path)
            self.assertEqual(seen, ['/redirect', '/forbidden'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_offline_cli_recomputes_snapshot_without_credentials_or_network(self):
        from evals.runtime import main
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.json'
            source.write_text(json.dumps({'provenance': {'start_time': 'start', 'end_time': 'end'},
                                          'spans': [span()]}))
            output = Path(tmp) / 'results'
            with patch('sys.argv', ['runtime', '--input', str(source), '--output', str(output)]), \
                    patch('evals.runtime.API') as api, patch('builtins.print'):
                main()
            api.assert_not_called()
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(report['summary']['spans'], 1)
            self.assertEqual(len(report['provenance']['snapshot_sha256']), 64)
            self.assertTrue((output / 'summary.md').is_file())

    def test_partial_annotation_write_fails_instead_of_claiming_success(self):
        from evals.runtime import annotate
        api = Mock()
        api.call.return_value = {'data': []}
        with self.assertRaisesRegex(ValueError, 'confirm'):
            annotate(api, [span()])


if __name__ == '__main__':
    unittest.main()
