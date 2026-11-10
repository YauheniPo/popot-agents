import io
import ssl
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from popot_agents.runtime_config import RUNTIME
from popot_agents.worker.http_harness import _model_request


class NetworkRecoveryTests(unittest.TestCase):
    def request(self, response, budget=300, final=False, retries=20):
        now, sleeps, calls = [0.0], [], []
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        def perform(call, timeout):
            calls.append((now[0], timeout))
            return response(now, timeout)
        with patch.dict(RUNTIME['model'], {'request_retries': retries, 'request_retry_window_seconds': 180}), \
             patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: now[0]), \
             patch('popot_agents.worker.http_harness.time.sleep', side_effect=sleep), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=perform), \
             redirect_stderr(io.StringIO()):
            try:
                result = _model_request(object(), round_number=1, messages=[], schemas=[],
                    remaining_seconds=lambda: budget - now[0], final=final)
            except RuntimeError as error:
                result = error
        return result, now[0], sleeps, calls

    def test_minute_outage_recovers_with_backoff_for_work_and_final_requests(self):
        for final in (False, True):
            with self.subTest(final=final):
                def response(now, timeout):
                    if now[0] < 60:
                        raise URLError(OSError('network unreachable'))
                    return io.BytesIO(b'{"choices": []}')
                result, elapsed, sleeps, calls = self.request(response, final=final)
                self.assertEqual(result, {'choices': []})
                self.assertGreaterEqual(elapsed, 60)
                self.assertLessEqual(elapsed, 180)
                self.assertTrue(sleeps)
                self.assertLessEqual(max(sleeps), 15)
                self.assertLessEqual(len(calls), 15)

    def test_retry_window_and_parent_deadline_bound_waits(self):
        for budget, expected_limit in [(300, 180), (9, 9)]:
            with self.subTest(budget=budget):
                def response(now, timeout):
                    raise HTTPError('http://model', 503, 'unavailable', {}, io.BytesIO())
                result, elapsed, sleeps, calls = self.request(response, budget=budget)
                self.assertIsInstance(result, RuntimeError)
                self.assertTrue(sleeps)
                self.assertLessEqual(elapsed, expected_limit)
                self.assertTrue(all(start + timeout <= budget for start, timeout in calls))

    def test_permanent_http_error_is_not_retried(self):
        def response(now, timeout):
            raise HTTPError('http://model', 401, 'denied', {}, io.BytesIO())
        result, elapsed, sleeps, calls = self.request(response)
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_certificate_failure_is_not_retried(self):
        def response(now, timeout):
            raise URLError(ssl.SSLCertVerificationError('invalid certificate'))
        result, _, sleeps, calls = self.request(response)
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

