import base64
from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from ax_local.worker import remote_client


class ChunkedHistoryTests(unittest.TestCase):
    def test_large_unicode_history_reaches_worker_without_truncation(self):
        payload = {'action': 'restore', 'messages': [
            {'role': 'user', 'content': 'я' * 190000},
            {'role': 'assistant', 'content': 'готово'},
        ]}
        encoded = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(remote_client, 'REQUEST_DIRECTORY', directory, create=True), \
             patch.object(remote_client, 'call', return_value={'status': 'ok'}) as call, \
             redirect_stdout(io.StringIO()):
            request_id = 'a' * 32
            for offset in range(0, len(encoded), 48000):
                remote_client.main(['--chunk', request_id, encoded[offset:offset + 48000]])
            remote_client.main(['--consume', request_id])
            received = json.loads(base64.urlsafe_b64decode(call.call_args.args[0]))
            self.assertEqual(received, payload)
            from pathlib import Path
            self.assertEqual(list(Path(directory).iterdir()), [])
