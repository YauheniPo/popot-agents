import hashlib
import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from popot_agents.runtime_config import RUNTIME
from popot_agents.skills import prepare_role_config


ROOT = Path(__file__).resolve().parents[1]


class SkillImportTests(unittest.TestCase):
    def test_manifest_covers_every_imported_file_and_license(self):
        root = ROOT / 'skills'
        manifest = json.loads((root / 'upstream.json').read_text())
        self.assertRegex(manifest['commit'], r'^[0-9a-f]{40}$')
        self.assertEqual(len(manifest['skills']), len(set(manifest['skills'])))
        files = {}
        for name in manifest['skills']:
            self.assertRegex(name, r'^[a-z0-9_-]+/[a-z0-9_-]+$')
            directory = root / name
            self.assertTrue((directory / 'SKILL.md').is_file(), name)
            for path in directory.rglob('*'):
                self.assertFalse(path.is_symlink(), str(path))
                if path.is_file():
                    files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(files, manifest['sha256'])
        self.assertEqual(hashlib.sha256((root / 'LICENSE.mattpocock').read_bytes()).hexdigest(),
                         manifest['license_sha256'])
        skill_directories = {path.parent.relative_to(root).as_posix()
                             for path in root.rglob('SKILL.md')}
        self.assertEqual(skill_directories, set(manifest['skills']))

    def test_planner_discussion_loads_domain_and_tracker_references_within_budget(self):
        planner = json.loads((ROOT / 'config/roles/planner.json').read_text())
        self.assertTrue({'engineering/grill-with-docs', 'productivity/grilling',
                         'engineering/domain-modeling', 'engineering/setup-matt-pocock-skills',
                         'engineering/to-spec', 'engineering/to-tickets',
                         'engineering/code-review', 'productivity/handoff',
                         'engineering/retro', 'productivity/writing-for-agents'} <= set(planner['skills']))
        with redirect_stderr(io.StringIO()):
            prepared = prepare_role_config(planner)
        content = prepared['instructions']
        for document in ('GLOSSARY-FORMAT.md', 'ADR-FORMAT.md', 'issue-tracker-local.md',
                         'engineering/retro/SKILL.md', 'SKILL-MECHANICS.md'):
            self.assertIn(document, content)
        self.assertIn('disable-model-invocation: true',
                      (ROOT / 'skills/engineering/retro/SKILL.md').read_text())
        self.assertLessEqual(len(planner['instructions']), 4000)
        loaded = content[len(planner['instructions']):]
        self.assertLessEqual(len(loaded.encode('utf-8')), RUNTIME['limits']['skill_prompt_bytes'])
        self.assertIn('Tracker defaults to local', planner['instructions'])
        self.assertIn('setup references', planner['instructions'])
        self.assertIn('delegate_task', planner['instructions'])
        self.assertNotIn('engineering/implement-spec', planner['skills'])

    def test_engineers_load_pr_and_agent_writing_references_without_changing_role_settings(self):
        for name in ('backend_engineer', 'frontend_engineer'):
            with self.subTest(role=name):
                role = json.loads((ROOT / 'config/roles' / f'{name}.json').read_text())
                self.assertTrue({'engineering/pr', 'productivity/writing-for-agents'} <= set(role['skills']))
                with redirect_stderr(io.StringIO()):
                    prepared = prepare_role_config(role)
                for document in ('engineering/pr/SKILL.md', 'engineering/pr/CREDITS.md',
                                 'productivity/writing-for-agents/SKILL-MECHANICS.md'):
                    self.assertIn(document, prepared['instructions'])
                self.assertEqual({key: value for key, value in prepared.items() if key != 'instructions'},
                                 {key: value for key, value in role.items() if key not in {'instructions', 'skills'}})
                loaded = prepared['instructions'][len(role['instructions']):]
                self.assertLessEqual(len(loaded.encode('utf-8')), RUNTIME['limits']['skill_prompt_bytes'])
                self.assertIn('skills', role)


if __name__ == '__main__':
    unittest.main()
