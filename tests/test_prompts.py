"""Prompt configuration preserves text and treats substitutions as plain data."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class PromptConfigTests(unittest.TestCase):
    def test_operator_reports_require_primary_sources_and_metric_scope(self):
        from popot_agents.prompts import render_prompt
        role = json.loads((Path(__file__).resolve().parents[1] / 'config/roles/project_operator.json').read_text())
        for prompt in (role['instructions'], render_prompt('holmes/diagnosis')):
            for text in ('25m', 'exit code', 'requests/limits', 'utilization', 'timestamp'):
                self.assertIn(text, prompt)
        for text in ('no_data', 'source_type=analysis', 'warm_workers', 'connection count'):
            self.assertIn(text, role['instructions'])
        extraction = render_prompt('memory/extract', max_memories=5)
        self.assertIn('evidence_sources', extraction)
        self.assertIn('analysis', extraction)
        self.assertIn('no_data', extraction)

    def test_cached_child_reconciliation_requires_an_explicit_assignment_link(self):
        from popot_agents.prompts import render_prompt
        instructions = render_prompt('delegation/instructions', role='planner', review_routing='', description='')
        self.assertIn('completed replacement', instructions)
        self.assertIn('replaces_session_id', instructions)
        self.assertIn('exact original task', instructions)
        self.assertIn('Never start a replacement for a resumable child', instructions)

    def test_corrections_are_not_forbidden_by_the_expired_initial_window(self):
        from popot_agents.prompts import render_prompt
        budget = render_prompt('workflow/budget', budget='{}')
        for text in ('implementation_limited=false', 'zero', 'required corrections',
                     'workflow_seconds_remaining', 'hard deadline'):
            self.assertIn(text, budget)

    def test_sonar_pr_binding_uses_pr_specific_revision_instead_of_main_analyses(self):
        from popot_agents.prompts import render_prompt
        inspection = render_prompt('tools/inspection')
        for text in ('project_pull_requests/list', 'commit.sha', 'analysisDate',
                     'project_analyses', 'does not prove'):
            self.assertIn(text, inspection)

    def test_exported_reports_cannot_claim_a_newer_revision_than_the_assessment(self):
        from popot_agents.prompts import render_prompt
        contract = render_prompt('reporting/result_contract', artifact_count_limit=16,
                                 artifact_byte_limit=24000)
        for text in ('report artifacts', 'structured revision', 'superseded', 'PR metadata'):
            self.assertIn(text, contract)

    def test_missing_prerequisites_require_revision_bound_ci_and_test_guard_evidence(self):
        from popot_agents.prompts import render_prompt
        inspection = render_prompt('tools/inspection')
        coordinator = render_prompt('workflow/coordinator')
        for text in ('exact revision', 'CI services', 'test guards', 'opt-in',
                     'already configured', 'new external service'):
            self.assertIn(text, inspection)
        for text in ('missing prerequisite', 'observed failure', 'configured CI',
                     'routine', 'uncovered lines', 'number of tests'):
            self.assertIn(text, coordinator)

    def test_review_assignment_does_not_follow_a_moving_pr_head_implicitly(self):
        from popot_agents.prompts import render_prompt
        review = render_prompt('review/verification')
        coordinator = render_prompt('workflow/coordinator')
        for text in ('assigned revision', 'PR head moves', 'completed', 'current head'):
            self.assertIn(text, review)
        for text in ('revision=', 'new revision', 'verification_status=failed',
                     'completed assessment', 'implementation owner'):
            self.assertIn(text, coordinator)

    def test_qa_prepares_project_dependencies_and_preserves_node_skip_evidence(self):
        from popot_agents.prompts import render_prompt
        qa = render_prompt('delegation/qa')
        for value in ('python -m venv', 'requirements', 'same interpreter', '--test-reporter=tap',
                      'WORKSPACE_UPSTREAM_DIR', 'skip', 'tail'):
            self.assertIn(value, qa)

    def test_repository_readback_and_summary_only_verification_are_distinguished(self):
        from popot_agents.prompts import render_prompt
        contract = render_prompt('reporting/result_contract', artifact_count_limit=16,
                                 artifact_byte_limit=24000)
        coordinator = render_prompt('workflow/coordinator')
        for text in ('get_file_contents', 'repo://', 'blob SHA', 'Python'):
            self.assertIn(text, contract)
        for text in ('summary-only', 'analysis-only', 'already observed'):
            self.assertIn(text, coordinator)

    def test_result_contract_requires_scoped_findings_and_same_call_check_provenance(self):
        from popot_agents.prompts import render_prompt
        contract = render_prompt('reporting/result_contract', artifact_count_limit=16,
                                 artifact_byte_limit=24000)
        for text in ('"findings":[]', 'in_scope', 'out_of_scope', 'rationale',
                     'Minor', 'HEAD=', 'DIRTY=', 'test exit code'):
            self.assertIn(text, contract)

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

    def test_completion_prompts_keep_actionable_findings_in_the_workflow(self):
        from popot_agents.prompts import render_prompt
        coordinator = render_prompt('workflow/coordinator')
        contract = render_prompt('reporting/result_contract', artifact_count_limit=16,
                                 artifact_byte_limit=24000)
        final = render_prompt('reporting/final', result_contract=contract)
        for prompt in (coordinator, contract, final):
            self.assertIn('remaining_work', prompt)
            self.assertIn('in-scope', prompt)
        self.assertIn('stale published', coordinator)
        self.assertIn('Do not offer', contract)

    def test_delegated_review_completion_is_scoped_to_its_assignment(self):
        from popot_agents.prompts import render_prompt
        reviewer = render_prompt('delegation/reviewer')
        checkpoint = render_prompt('review/checkpoint_instructions')
        coordinator = render_prompt('workflow/coordinator')
        contract = render_prompt('reporting/result_contract', artifact_count_limit=16,
                                 artifact_byte_limit=24000)
        final = render_prompt('reporting/final', result_contract=contract)
        self.assertIn('For a delegated review-only assignment', reviewer)
        self.assertIn('completed', reviewer)
        self.assertIn('findings in answer and evidence', reviewer)
        self.assertIn('your assigned review', checkpoint)
        self.assertIn('acceptance of your assigned deliverables', coordinator)
        self.assertIn('A delegated result describes only its assigned deliverables', contract)
        self.assertIn('assigned deliverables', final)
        self.assertIn('parent owns unresolved implementation and acceptance work', contract)
        self.assertIn('verification_status=failed', contract)
        self.assertIn('verification_status=passed', contract)
        self.assertIn('verification_status', reviewer)
        for prompt in (contract, final.removesuffix(contract).strip(), coordinator):
            self.assertIn('owned by your assignment', prompt)
            self.assertIn('another owner', prompt)

    def test_publication_freshness_and_readiness_have_explicit_owners(self):
        from popot_agents.prompts import render_prompt
        coordinator = render_prompt('workflow/coordinator')
        contract = render_prompt('reporting/result_contract', artifact_count_limit=16,
                                 artifact_byte_limit=24000)
        self.assertIn('existing PR', coordinator)
        self.assertIn('publication owner', coordinator)
        self.assertIn('existing verified-revision and test claims', coordinator)
        self.assertIn('checks actually observed on that SHA', coordinator)
        self.assertIn('read back the PR body and head together', coordinator)
        self.assertIn('Carry this obligation into continuations', coordinator)
        self.assertIn('Do not create a publication solely', coordinator)
        self.assertIn('delegated implementer', contract)
        self.assertIn('verification owner still owes review or QA', contract)

    def test_workflow_budget_keeps_verification_inside_the_original_deadline(self):
        from popot_agents.prompts import render_prompt
        budget = json.dumps({'workflow_seconds_remaining': 900,
                             'implementation_seconds_remaining': 0,
                             'verification_reserve_seconds': 900,
                             'implementation_limited': True,
                             'verification_started': False})
        prompt = render_prompt('workflow/budget', budget=budget)
        self.assertIn(budget, prompt)
        self.assertIn('checkpoint', prompt)
        self.assertIn('independent review and QA', prompt)
        self.assertIn('original workflow deadline', prompt)
        self.assertIn('not a passing verdict', prompt)
        self.assertIn('Do not start optional implementation', prompt)

    def test_last_coordinator_turn_can_delegate_remaining_verification(self):
        from popot_agents.prompts import render_prompt
        prompt = render_prompt('workflow/coordinator')
        self.assertIn('last coordinator turn', prompt)
        self.assertIn('await_tasks preserves the same coordinator turn', prompt)
        self.assertIn('shared deadline, call limits and setup checks', prompt)
        self.assertNotIn('no new child starts then', prompt)

    def test_delegation_distinguishes_reused_results_and_startup_failures(self):
        from popot_agents.prompts import render_prompt
        prompt = render_prompt('delegation/instructions', role='planner',
                               review_routing='', description='Specialists')
        self.assertIn('continuation_skipped=true', prompt)
        self.assertIn('new task without session_id', prompt)
        self.assertIn('rejected before a child session exists', prompt)
        self.assertIn('not a worker startup failure', prompt)
        self.assertIn('completed review does not certify merge readiness', prompt)

    def test_result_reporting_preserves_uncertainty_in_missing_receipts(self):
        from popot_agents.prompts import render_prompt
        contract = render_prompt('reporting/result_contract', artifact_count_limit=16,
                                 artifact_byte_limit=24000)
        self.assertIn('Read the available tool receipts before summarizing child progress', contract)
        self.assertIn('missing or truncated evidence', contract)
        self.assertIn('does not prove a check never ran', contract)
