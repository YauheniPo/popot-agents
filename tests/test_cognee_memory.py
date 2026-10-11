import json
import threading
import unittest
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from popot_agents.worker.cognee_memory import CogneeMemory, MemoryServiceError


class CogneeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.failure = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.respond()

            def do_GET(self):
                self.respond()

            def respond(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                owner.calls.append((self.path, dict(self.headers), body))
                status, result = 200, {}
                if self.path == '/api/v1/auth/login':
                    owner.assertEqual(parse_qs(body.decode())['password'], ['private-password'])
                    result = {'access_token': 'private-token'}
                elif owner.failure:
                    status, result = owner.failure, {'detail': 'private-password private-token'}
                else:
                    owner.assertEqual(self.headers['Authorization'], 'Bearer private-token')
                    if self.path == '/api/v1/remember':
                        result = {'status': 'running', 'pipeline_run_id': 'run-1'}
                    elif self.path == '/api/v1/datasets':
                        result = [{'id': 'project-id', 'name': 'project-a'},
                                  {'id': 'other-id', 'name': 'project-b'}]
                    elif self.path.startswith('/api/v1/datasets/status'):
                        result = {'project-id': 'DATA_PROCESSED'}
                    elif self.path == '/api/v1/recall':
                        result = [{'text': 'Verified decision'}]
                payload = json.dumps(result).encode()
                self.send_response(status)
                if status == 307:
                    self.send_header('Location', '/leaked-credentials')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.memory = CogneeMemory(f'http://127.0.0.1:{self.server.server_port}',
                                   'private-password')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_session_event_uses_authenticated_json_endpoint(self):
        event = {'session_id': 'popot-test', 'dataset_name': 'project-a', 'status': 'running'}
        self.memory.session_event(event)
        path, headers, body = self.calls[-1]
        self.assertEqual(path, '/api/v1/popot/session')
        self.assertEqual(json.loads(body), event)
        self.assertEqual(headers['Authorization'], 'Bearer private-token')

    def test_remember_is_durable_scoped_and_reports_background_work(self):
        result = self.memory.remember('Decision: use PostgreSQL.', 'project-a')
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['pipeline_run_id'], 'run-1')
        path, headers, body = self.calls[-1]
        self.assertEqual(path, '/api/v1/remember')
        self.assertIn('multipart/form-data', headers['Content-Type'])
        for value in (b'name="datasetName"', b'project-a', b'name="run_in_background"',
                      b'true', b'name="self_improvement"', b'false',
                      b'filename="memory_', b'Decision: use PostgreSQL.'):
            self.assertIn(value, body)
        self.assertNotIn(b'session_id', body)
        self.assertNotIn('private-password', json.dumps(result))

    def test_distinct_memories_have_distinct_upload_names_and_retries_reuse_names(self):
        import re
        names = []
        for text in ('First decision', 'Second decision', 'First decision'):
            self.memory.remember(text, 'project-a')
            names.append(re.search(rb'filename="([^"]+)"', self.calls[-1][2]).group(1))
        self.assertNotEqual(names[0], names[1])
        self.assertEqual(names[0], names[2])

    def test_record_metadata_is_not_indexed_as_knowledge(self):
        record = {'project': 'project-a', 'recorded_at': '2026-10-10T11:45:06+00:00',
                  'agent_model': 'openrouter/free', 'references': ['https://github.com/a/b/pull/70'],
                  'trust': 'historical; verify', 'memories': [
                      {'kind': 'decision', 'text': 'PR #70 uses a 60-second retry for HTTP 429.',
                       'source': 'call-private-id', 'evidence_quote': 'RETRY_SECONDS = 60'}]}
        self.memory.remember(json.dumps(record), 'project-a')
        _, headers, body = self.calls[-1]
        message = BytesParser(policy=default).parsebytes(
            ('Content-Type: ' + headers['Content-Type'] + '\r\n\r\n').encode() + body)
        parts = {p.get_param('name', header='content-disposition'): p.get_payload(decode=True).decode()
                 for p in message.iter_parts()}
        self.assertIn('60-second retry for HTTP 429', parts['data'])
        for value in ('2026-10-10', 'openrouter/free', 'call-private-id', 'evidence_quote'):
            self.assertNotIn(value, parts['data'])
        metadata = json.loads(parts['external_metadata'])[0]['popot_memory']
        self.assertEqual(metadata['record'], record)
        self.assertEqual(metadata['version'], 2)
        self.assertEqual(json.loads(parts['labels']), [metadata['title']])

    def test_malformed_record_fails_without_silently_losing_entries(self):
        for items in ([], [{'kind': 'fact', 'text': ''}],
                      [{'kind': 'fact', 'text': 'Valid'}, {'text': 7}]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.memory.remember(json.dumps({'project': 'project-a', 'memories': items}), 'project-a')
        self.assertEqual(self.calls, [])

    def test_recall_only_searches_requested_project_without_llm_completion(self):
        self.assertEqual(self.memory.recall('database decision', 'project-a'),
                         [{'text': 'Verified decision'}])
        payload = json.loads(self.calls[-1][2])
        self.assertEqual(payload, {'query': 'database decision', 'datasets': ['project-a'],
                                   'search_type': 'CHUNKS', 'top_k': 5})

    def test_empty_project_does_not_fall_back_to_other_projects(self):
        result = self.memory.recall('anything', 'missing-project')
        self.assertEqual(result, {'status': 'empty', 'memories': []})
        self.assertFalse(any(path == '/api/v1/recall' for path, _, _ in self.calls))

    def test_status_is_scoped_to_project_id(self):
        self.assertEqual(self.memory.status('project-a'),
                         {'project-id': 'DATA_PROCESSED'})
        self.assertEqual(self.calls[-1][0], '/api/v1/datasets/status?dataset=project-id')

    def test_errors_do_not_expose_password_or_bearer_and_redirects_are_not_followed(self):
        for status in (401, 500, 307):
            with self.subTest(status=status):
                self.failure = status
                with self.assertRaises(MemoryServiceError) as raised:
                    self.memory.remember('Fact', 'project-a')
                self.assertIn(str(status), str(raised.exception))
                self.assertNotIn('private-password', str(raised.exception))
                self.assertNotIn('private-token', str(raised.exception))
        self.assertFalse(any(path == '/leaked-credentials' for path, _, _ in self.calls))

    def test_invalid_input_fails_before_authentication(self):
        for data, dataset in [('', 'project-a'), ('Fact', ''), ('Fact', '../other')]:
            with self.subTest(data=data, dataset=dataset):
                with self.assertRaises(ValueError):
                    self.memory.remember(data, dataset)
        with self.assertRaises(ValueError):
            self.memory.recall('query', 'project-a', top_k=0)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
