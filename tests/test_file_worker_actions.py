"""Non-export file actions keep working after the artifacts branch was added."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popot_agents.worker import file_worker


class FileWorkerReadTests(unittest.TestCase):
    def test_read_action_returns_file_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'note.txt').write_text('hello', encoding='utf-8')
            output = io.StringIO()
            with patch.object(file_worker, 'WORKSPACE_ROOT', root), \
                    patch.object(file_worker, 'workspace_path', return_value=root / 'note.txt'), \
                    patch('sys.argv', ['file_worker.py', 'read']), \
                    patch('sys.stdin', io.StringIO(json.dumps({'path': 'note.txt'}))), \
                    patch('sys.stdout', output):
                file_worker.main()
            self.assertEqual(output.getvalue(), 'hello')


if __name__ == '__main__':
    unittest.main()
