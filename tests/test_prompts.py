"""Prompt configuration preserves text and treats substitutions as plain data."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class PromptConfigTests(unittest.TestCase):
    def test_substitution_is_strict_and_not_recursive(self):
        from popot_agents import prompts
        with tempfile.TemporaryDirectory() as directory, patch.object(prompts, 'PROMPTS_ROOT', Path(directory)):
            Path(directory, 'example.md').write_text('Role: {{role}}\nJSON: {"ok":true}\n', encoding='utf-8')
            self.assertIn('{{other}}', prompts.render_prompt('example', role='{{other}}'))
            self.assertIn('{"ok":true}', prompts.render_prompt('example', role='QA'))
            with self.assertRaisesRegex(ValueError, 'role'):
                prompts.render_prompt('example')
            with self.assertRaisesRegex(ValueError, 'extra'):
                prompts.render_prompt('example', role='QA', extra='unused')

    def test_missing_empty_and_external_templates_fail_explicitly(self):
        from popot_agents import prompts
        with tempfile.TemporaryDirectory() as directory, patch.object(prompts, 'PROMPTS_ROOT', Path(directory)):
            Path(directory, 'empty.md').write_text(' \n')
            for name in ('missing', 'empty', '../outside', '/tmp/outside'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    prompts.render_prompt(name)

    def test_migrated_text_matches_pre_migration_baseline(self):
        from popot_agents.prompts import render_prompt
        fixture = json.loads((Path(__file__).parent / 'fixtures/prompt_baseline.json').read_text())
        for name, expected in fixture.items():
            with self.subTest(name=name):
                rendered = render_prompt(name, **expected.get('values', {}))
                digest = hashlib.sha256(' '.join(rendered.split()).encode()).hexdigest()
                self.assertEqual(digest, expected['sha256'])

    def test_delegation_policy_uses_configuration(self):
        from popot_agents import prompts
        from popot_agents.worker.delegation_policy import delegation_instructions
        with tempfile.TemporaryDirectory() as directory, patch.object(prompts, 'PROMPTS_ROOT', Path(directory)):
            folder = Path(directory, 'delegation')
            folder.mkdir()
            (folder / 'instructions.md').write_text('Configured {{role}}; {{review_routing}}; {{description}}')
            tools = {'delegate_task': {'schema': {'function': {'description': 'QA capability',
                'parameters': {'properties': {'role': {'enum': ['qa_engineer']}}}}}}}
            self.assertEqual(delegation_instructions({'name': 'engineer'}, tools),
                             'Configured engineer; ; QA capability')
