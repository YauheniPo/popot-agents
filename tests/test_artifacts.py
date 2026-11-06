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
    def export_workspace(self, root):
        from popot_agents import tools
        if os.geteuid() == 0:
            os.chown(root, 10001, 10001)
        with patch.object(tools, 'WORKSPACE_ROOT', root):
            return tools.export_artifacts(timeout_seconds=3)

    def test_subprocess_export_explains_byte_limit_and_recovers_after_moving_inputs(self):
        from popot_agents import tools
        from popot_agents.runtime_config import RUNTIME
        limit = RUNTIME['limits']['tool_output_chars']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'artifacts'
            target.mkdir()
            (target / 'fix.patch').write_text('patch')
            raw = target / 'research.json'
            raw.write_bytes(b'x' * (limit + 1))
            with self.assertRaises(tools.ToolExecutionError) as caught:
                self.export_workspace(root)
            message = str(caught.exception)
            self.assertIn(f'{limit} bytes', message)
            self.assertIn(f'at least {limit + 1} bytes', message)
            self.assertIn('outside /workspace/artifacts', message)
            raw.rename(root / raw.name)
            self.assertEqual(self.export_workspace(root), [{'name': 'fix.patch', 'content': 'patch',
                'sha256': hashlib.sha256(b'patch').hexdigest()}])

    def test_subprocess_export_explains_file_count_limit(self):
        from popot_agents import tools
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'artifacts'
            target.mkdir()
            for index in range(17):
                (target / f'{index}.log').write_text('check')
            with self.assertRaises(tools.ToolExecutionError) as caught:
                self.export_workspace(root)
            self.assertIn('16', str(caught.exception))
            self.assertIn('at least 17 files', str(caught.exception))

    def test_subprocess_export_explains_utf8_failure_without_file_data(self):
        from popot_agents import tools
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'artifacts'
            target.mkdir()
            (target / 'PRIVATE_FILENAME.log').write_bytes(b'\xffPRIVATE_DATA')
            with self.assertRaises(tools.ToolExecutionError) as caught:
                self.export_workspace(root)
            self.assertIn('UTF-8', str(caught.exception))
            self.assertNotIn('PRIVATE_DATA', str(caught.exception))
            self.assertNotIn('PRIVATE_FILENAME', str(caught.exception))

    def test_subprocess_export_does_not_forward_arbitrary_error_output(self):
        from popot_agents import tools
        for output in (b'PRIVATE_DATA', b'{"artifact_error":"PRIVATE_DATA"}',
                       b'{"artifact_error":"byte_limit","minimum":"PRIVATE_DATA"}'):
            with self.subTest(output=output), patch.object(tools.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 2, stdout=output, stderr=b'PRIVATE_DATA')):
                with self.assertRaises(tools.ToolExecutionError) as caught:
                    tools._file_action('export_artifacts', {})
                self.assertNotIn('PRIVATE_DATA', str(caught.exception))

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

