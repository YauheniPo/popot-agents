import hashlib
import io
import json
import re
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
        imported = set()
        for source in [manifest, *manifest.get('additional_sources', [])]:
            with self.subTest(repository=source['repository']):
                self.assertRegex(source['commit'], r'^[0-9a-f]{40}$')
                self.assertEqual(len(source['skills']), len(set(source['skills'])))
                self.assertTrue(imported.isdisjoint(source['skills']))
                imported.update(source['skills'])
                files = {}
                for name in source['skills']:
                    self.assertRegex(name, r'^[a-z0-9_-]+/[a-z0-9_-]+$')
                    directory = root / name
                    self.assertTrue((directory / 'SKILL.md').is_file(), name)
                    for path in directory.rglob('*'):
                        self.assertFalse(path.is_symlink(), str(path))
                        if path.is_file():
                            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(files, source['sha256'])
                license_path = root / source.get('license_file', 'LICENSE.mattpocock')
                self.assertEqual(hashlib.sha256(license_path.read_bytes()).hexdigest(),
                                 source['license_sha256'])
        skill_directories = {path.parent.relative_to(root).as_posix()
                             for path in root.rglob('SKILL.md')}
        # Project-authored skills are not part of the third-party provenance manifest.
        local_skills = {'engineering/security-review'}
        self.assertTrue(local_skills.isdisjoint(imported))
        self.assertEqual(skill_directories, imported | local_skills)

    def test_code_simplifier_provenance_and_entrypoint_links(self):
        root = ROOT / 'skills'
        manifest = json.loads((root / 'upstream.json').read_text())
        sources = [source for source in manifest.get('additional_sources', [])
                   if source['repository'] == 'https://github.com/pproenca/dot-skills']
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source['source_directory'], 'skills/.curated/code-simplifier')
        self.assertEqual(source['skills'], ['engineering/code-simplifier'])
        self.assertEqual(source['license_file'], 'LICENSE.pproenca')
        directory = root / 'engineering/code-simplifier'
        links = re.findall(r'\]\(([^)#]+)(?:#[^)]*)?\)', (directory / 'SKILL.md').read_text())
        self.assertTrue(links)
        for link in links:
            if '://' not in link:
                self.assertTrue((directory / link).is_file(), link)

    def test_code_simplifier_loads_only_for_code_roles_without_changing_permissions(self):
        selected = set()
        for path in (ROOT / 'config/roles').glob('*.json'):
            role = json.loads(path.read_text())
            if 'engineering/code-simplifier' not in role.get('skills', []):
                continue
            selected.add(path.stem)
            with self.subTest(role=path.stem):
                with redirect_stderr(io.StringIO()):
                    prepared = prepare_role_config(role)
                skill = ROOT / 'skills/engineering/code-simplifier'
                self.assertIn((skill / 'SKILL.md').read_text(), prepared['instructions'])
                self.assertIn(str(skill / 'references/behave-no-semantics-change.md'),
                              prepared['instructions'])
                self.assertEqual({key: value for key, value in prepared.items() if key != 'instructions'},
                                 {key: value for key, value in role.items() if key not in {'instructions', 'skills'}})
                loaded = prepared['instructions'][len(role['instructions']):]
                self.assertLessEqual(len(loaded.encode('utf-8')), RUNTIME['limits']['skill_prompt_bytes'])
        self.assertEqual(selected, {'backend_engineer', 'frontend_engineer', 'qa_engineer', 'code_reviewer'})

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
