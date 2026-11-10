"""Unpublished deliverables must outlive the worker that produced them."""
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ArtifactTests(unittest.TestCase):
    def test_export_preserves_patch_bytes_and_ignores_other_workspace_files(self):
        from popot_agents.artifacts import collect_artifacts
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(collect_artifacts(root), [])
            (root / 'artifacts').mkdir()
            content = 'diff --git a/check.py b/check.py\r\n+print("проверка")\r\n'
            data = content.encode()
            (root / 'artifacts' / 'fix.patch').write_bytes(data)
            (root / '.env').write_text('PRIVATE_DATA')
            self.assertEqual(collect_artifacts(root), [{'name': 'fix.patch', 'content': content,
                'sha256': hashlib.sha256(data).hexdigest()}])

    def test_unsafe_or_incomplete_exports_fail_without_a_truncated_success(self):
        from popot_agents.artifacts import ArtifactError, collect_artifacts
        from popot_agents.runtime_config import RUNTIME
        for kind in ('directory_link', 'file_link', 'directory', 'binary', 'large', 'many', 'hidden', 'fifo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                outside = root / 'private'
                outside.write_text('PRIVATE_DATA')
                target = root / 'artifacts'
                if kind == 'directory_link': target.symlink_to(root, target_is_directory=True)
                else:
                    target.mkdir()
                    path = target / 'fix.patch'
                    if kind == 'file_link': path.symlink_to(outside)
                    elif kind == 'directory': path.mkdir()
                    elif kind == 'binary': path.write_bytes(b'\xff')
                    elif kind == 'large': path.write_bytes(b'x' * (RUNTIME['limits']['tool_output_chars'] + 1))
                    elif kind == 'many':
                        for index in range(17): (target / f'{index}.patch').write_text('x')
                    elif kind == 'hidden': (target / '.env').write_text('PRIVATE_DATA')
                    elif kind == 'fifo': os.mkfifo(path)
                with self.assertRaises(ArtifactError) as caught:
                    collect_artifacts(root)
                self.assertNotIn('PRIVATE_DATA', str(caught.exception))

    def test_validator_rejects_corrupted_content_and_duplicate_names(self):
        from popot_agents.artifacts import ArtifactError, validate_artifacts
        from popot_agents.runtime_config import RUNTIME
        artifact = {'name': 'fix.patch', 'content': 'patch',
                    'sha256': hashlib.sha256(b'patch').hexdigest()}
        self.assertEqual(validate_artifacts([artifact]), [artifact])
        large = 'x' * (RUNTIME['limits']['tool_output_chars'] + 1)
        for invalid in ([{**artifact, 'content': 'changed'}], [artifact, artifact],
                        [{**artifact, 'name': '../private'}], [artifact] * 17,
                        [{**artifact, 'content': large, 'sha256': hashlib.sha256(large.encode()).hexdigest()}]):
            with self.subTest(invalid=invalid), self.assertRaises(ArtifactError):
                validate_artifacts(invalid)

    def test_file_worker_exports_exact_contents_through_its_protocol(self):
        from popot_agents.worker import file_worker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'artifacts').mkdir()
            (root / 'artifacts' / 'fix.patch').write_bytes(b'patch\r\n')
            output = io.StringIO()
            with patch.object(file_worker, 'WORKSPACE_ROOT', root), \
                 patch('sys.argv', ['file_worker.py', 'export_artifacts']), \
                 patch('sys.stdin', io.StringIO('{}')), patch('sys.stdout', output):
                file_worker.main()
            artifact = json.loads(output.getvalue())[0]
            self.assertEqual(artifact['content'], 'patch\r\n')
            self.assertEqual(artifact['sha256'], hashlib.sha256(b'patch\r\n').hexdigest())

    def test_export_wrapper_honors_deadline_and_reports_incomplete_transport(self):
        from popot_agents import tools
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'artifacts').mkdir()
            with patch.object(tools, 'WORKSPACE_ROOT', root):
                with patch.object(tools, '_file_action', return_value='[]') as action:
                    self.assertEqual(tools.export_artifacts(timeout_seconds=3), [])
                    self.assertEqual(action.call_args.args, ('export_artifacts', {}, 3))
                for error in (OSError('PRIVATE_DATA'), ValueError('PRIVATE_DATA'),
                              subprocess.TimeoutExpired('export', 3)):
                    with self.subTest(error=type(error).__name__), \
                         patch.object(tools, '_file_action', side_effect=error):
                        with self.assertRaises(tools.ToolExecutionError) as caught:
                            tools.export_artifacts(timeout_seconds=3)
                        self.assertNotIn('PRIVATE_DATA', str(caught.exception))

