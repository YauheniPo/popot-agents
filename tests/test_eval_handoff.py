"""A local bundle preserves the identity required by delegated verification."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from popot_agents.work_evidence import observe_tool_result
from popot_agents.worker.verification import observed_revision


@unittest.skipUnless(shutil.which('git'), 'requires local git for the fixture handoff')
class EvalHandoffTests(unittest.TestCase):
    def test_bundle_clone_verifies_the_parent_commit_without_recreating_it(self):
        def git(directory, *args):
            return subprocess.check_output(['git', *args], cwd=directory, text=True, stderr=subprocess.DEVNULL).strip()

        with tempfile.TemporaryDirectory() as tmp:
            parent, child, bundle = Path(tmp) / 'parent', Path(tmp) / 'child', Path(tmp) / 'review.bundle'
            parent.mkdir()
            git(parent, 'init', '-q')
            (parent / 'pagination.py').write_text('def paginate(items, page, size): return list(items)\n')
            git(parent, 'add', 'pagination.py')
            git(parent, '-c', 'user.name=Eval', '-c', 'user.email=eval@example.test', 'commit', '-qm',
                'Add fixture\n\nAssisted-by: Codex <noreply@openai.com>')
            revision = git(parent, 'rev-parse', 'HEAD')
            git(parent, 'bundle', 'create', str(bundle), 'HEAD')
            git(Path(tmp), 'clone', '-q', str(bundle), str(child))
            actual = git(child, 'rev-parse', 'HEAD')
            receipt = observe_tool_result('bash', {'command': 'git rev-parse HEAD'},
                                          'exit_code=0\n' + actual + '\n', 'read-head')
            self.assertEqual(actual, revision)
            self.assertEqual(observed_revision({'revision': revision}, [receipt], revision), revision)
            self.assertIsNone(observed_revision({'revision': 'f' * 40}, [receipt], 'f' * 40))
