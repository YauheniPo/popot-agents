import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ax_local.worker import remote_client


class ChunkedHistoryTests(unittest.TestCase):
    def test_chunk_file_is_private_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(remote_client, 'REQUEST_DIRECTORY', directory), \
             patch.object(remote_client, 'MAX_ENCODED_REQUEST_BYTES', 4), \
             redirect_stdout(io.StringIO()):
            request_id = 'b' * 32
            remote_client.main(['--chunk', request_id, 'YQ=='])
            path = Path(directory) / ('.ax-local-request-' + request_id)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, 'too large'):
                remote_client.main(['--chunk', request_id, 'Yg=='])
            self.assertEqual(path.read_text(), 'YQ==')

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'Requires no-follow file opens')
    def test_chunk_and_consume_do_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(remote_client, 'REQUEST_DIRECTORY', directory), \
             patch.object(remote_client, 'call') as call:
            target = Path(directory) / 'target'
            target.write_text('private-data')
            request_id = 'c' * 32
            path = Path(directory) / ('.ax-local-request-' + request_id)
            path.symlink_to(target)
            with self.assertRaises(OSError):
                remote_client.main(['--chunk', request_id, 'YQ=='])
            with self.assertRaises(OSError):
                remote_client.main(['--consume', request_id])
            self.assertEqual(target.read_text(), 'private-data')
            call.assert_not_called()

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
