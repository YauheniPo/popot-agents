import json
import os
from pathlib import Path
import tempfile
import unittest
import io
import sys
from unittest.mock import Mock, patch

from memory.import_skills import main, sync_skills


class CogneeSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        owner = self

        class Client:
            def _request(self, path, body):
                payload = json.loads(body)
                owner.calls.append((path, payload))
                return {'status': 'completed', 'items_processed': 1,
                        'items': [{'name': payload['skill_name'], 'kind': 'skill'}]}

        self.client = Client()

    def skill(self, name='engineering/review'):
        directory = self.root / name
        directory.mkdir(parents=True)
        (directory / 'SKILL.md').write_text(
            '---\nname: review\ndescription: Review code\n---\nRead checks.md.\n')
        return directory

    def test_imports_native_skills_with_references_and_stable_names(self):
        directory = self.skill()
        (directory / 'checks.md').write_text('Check failure scenarios.')
        (directory / 'tool.py').write_text('print("do not ingest executable")')
        self.assertEqual(sync_skills(self.root, self.client, 'project-a'), 1)
        path, payload = self.calls[0]
        self.assertEqual(path, '/api/v1/skills')
        self.assertEqual(payload['dataset_name'], 'project-a')
        self.assertEqual(payload['skill_name'], 'engineering--review')
        self.assertTrue(payload['skills_text'].startswith('---\nname: review'))
        self.assertIn('checks.md', payload['skills_text'])
        self.assertIn('Check failure scenarios.', payload['skills_text'])
        self.assertIn('skills/engineering/review/SKILL.md', payload['skills_text'])
        self.assertNotIn('do not ingest executable', payload['skills_text'])
        sync_skills(self.root, self.client, 'project-a')
        self.assertEqual(self.calls[0], self.calls[1])

    def test_validates_every_skill_before_any_upload(self):
        self.skill()
        broken = self.skill('engineering/z-broken')
        (broken / 'SKILL.md').write_text('')
        with self.assertRaisesRegex(ValueError, 'empty'):
            sync_skills(self.root, self.client, 'project-a')
        self.assertEqual(self.calls, [])

    def test_symlinks_and_invalid_datasets_are_rejected_before_upload(self):
        directory = self.skill()
        (directory / 'linked.md').symlink_to(directory / 'SKILL.md')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            sync_skills(self.root, self.client, 'project-a')
        with self.assertRaises(ValueError):
            sync_skills(self.root, self.client, '../other')
        self.assertEqual(self.calls, [])

    def test_failed_ingestion_is_not_reported_as_completed(self):
        self.skill()
        self.client._request = lambda *args: {'status': 'failed', 'items_processed': 0}
        with self.assertRaisesRegex(RuntimeError, 'not complete'):
            sync_skills(self.root, self.client, 'project-a')

    def test_import_timeout_comes_from_shared_config(self):
        from popot_agents.runtime_config import RUNTIME
        adapter = Mock()
        with patch.dict(sys.modules, {'cognee_memory': adapter}), \
             patch.dict(os.environ, {'DEFAULT_USER_PASSWORD': 'test-only'}), \
             patch.dict(RUNTIME['timeouts'], {'memory_skill_import_seconds': 487}), \
             patch.object(sys, 'argv', ['import_skills.py']), \
             patch('memory.import_skills.sync_skills', return_value=1), \
             patch('sys.stdout', new_callable=io.StringIO):
            main()
        self.assertEqual(adapter.CogneeMemory.call_args.kwargs['timeout'], 487)

    def test_progress_is_printed_before_the_upload(self):
        self.skill()
        original = self.client._request
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            def upload(path, body):
                self.assertIn('[1/1] engineering--review', output.getvalue())
                return original(path, body)
            self.client._request = upload
            sync_skills(self.root, self.client, 'project-a')

    def test_completed_upload_can_include_a_previous_timed_out_skill(self):
        self.skill()
        self.client._request = lambda *args: {'status': 'completed', 'items_processed': 2,
            'items': [{'name': 'engineering--review', 'kind': 'skill'},
                      {'name': 'previous', 'kind': 'skill'}]}
        self.assertEqual(sync_skills(self.root, self.client, 'project-a'), 1)

    def test_completed_upload_must_include_the_requested_skill(self):
        self.skill()
        self.client._request = lambda *args: {'status': 'completed', 'items_processed': 1,
            'items': [{'name': 'another', 'kind': 'skill'}]}
        with self.assertRaisesRegex(RuntimeError, 'not complete'):
            sync_skills(self.root, self.client, 'project-a')


if __name__ == '__main__':
    unittest.main()
