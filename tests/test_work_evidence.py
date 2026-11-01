import json
import os
import subprocess
import tempfile
import unittest

from popot_agents.tools import TOOL_SCHEMAS


class WorkEvidenceTests(unittest.TestCase):
    @unittest.skipUnless(os.getenv('AX_TEST_WORKER_IMAGE'), 'requires a built AX worker image')
    def test_shared_module_is_packaged_in_worker_image(self):
        result = subprocess.run(['docker','run','--rm','--entrypoint','python',
            os.environ['AX_TEST_WORKER_IMAGE'], '-c',
            'from popot_agents.worker.http_harness import run_http; from popot_agents.work_evidence import observe_tool_result'],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_schema_advertises_the_runtime_limit(self):
        schema = TOOL_SCHEMAS['bash']['function']
        self.assertEqual(schema['parameters']['properties']['command'].get('maxLength'), 4000)
        self.assertIn('write_file', schema['description'])

    def test_checks_receipt_requires_success_and_revision_in_same_result(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'a' * 40
        receipt = observe_tool_result('bash', {'command':'git rev-parse HEAD; python3 -m unittest'},
            f'exit_code=0\n{sha}\nRan 7 tests in 1.2s\nOK\n', 'checks')
        self.assertEqual(receipt['kind'], 'checks')
        self.assertEqual(receipt['status'], 'passed')
        self.assertEqual(receipt['revision'], sha)
        self.assertEqual(receipt['source'], 'tool_result')
        failed = observe_tool_result('bash', {'command':'python3 -m unittest'},
            'exit_code=1\nFAILED (failures=1)', 'bad')
        self.assertEqual(failed['status'], 'failed')
        self.assertNotIn('revision', failed)

    def test_observations_are_durable_without_overwriting_outcome(self):
        from popot_agents.work_evidence import observe_tool_result
        from popot_agents.orchestrator.session_store import SessionStore
        from popot_agents.orchestrator.work_status import build_work_status
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            sid = 'a'*16
            config = {'_workflow':{'state':'running','outcome':{'status':'partial',
                'evidence':[], 'remaining_work':['review'], 'errors':[]}}}
            store.save(sid, 'test', [], 'backend_engineer', config)
            receipt = observe_tool_result('bash', {'command':'git push'},
                'exit_code=0\nTo https://github.com/example/repo.git\n abc..def HEAD -> fix\n', 'push')
            store.record_evidence(sid, receipt)
            store.record_evidence(sid, receipt)
            store.save(sid, 'test', [], 'backend_engineer', config)
            saved = SessionStore(directory).get(sid)
            report = build_work_status(saved, [saved])['tasks'][0]
            self.assertEqual(len(report['milestones']), 1)
            self.assertEqual(report['milestones'][0]['kind'], 'publication')
            self.assertEqual(report['outcome']['status'], 'partial')

    def test_review_and_qa_have_distinct_verification_responsibilities(self):
        from popot_agents.prompts import render_prompt
        self.assertIn('full test suite', render_prompt('delegation/reviewer'))
        prompt = render_prompt('workflow/coordinator')
        self.assertIn('exact SHA', prompt)
        self.assertIn('commands', prompt)
        self.assertIn('Do not rerun an unchanged full test suite', render_prompt('delegation/qa'))

    def test_checks_for_other_or_unknown_revision_cannot_be_reused(self):
        from popot_agents.work_evidence import observe_tool_result, reusable_checks
        good = observe_tool_result('bash', {'command':'git rev-parse HEAD; python3 -m unittest'},
            'exit_code=0\n'+'a'*40+'\nRan 7 tests in 1s\nOK', 'check')
        self.assertEqual(reusable_checks([good], 'a'*40), [good])
        self.assertEqual(reusable_checks([good], 'b'*40), [])
        self.assertEqual(reusable_checks([good], None), [])

    def test_cli_readback_and_unknown_write_are_observations(self):
        from popot_agents.work_evidence import observe_tool_result, evidence_event
        receipt = observe_tool_result('bash', {'command':'gh pr view 80 --json headRefOid'},
            'exit_code=0\n'+json.dumps({'headRefOid':'c'*40}), 'head')
        self.assertEqual(receipt['revision'], 'c'*40)
        self.assertEqual(receipt['status'], 'observed')
        error = observe_tool_result('mcp__github__push_files', {},
            json.dumps({'isError':True,'outcome_unknown':True,'error':'network failed'}), 'write')
        self.assertEqual(error['status'], 'unknown')
        self.assertTrue(error['outcomeUnknown'])
        self.assertIsNone(evidence_event(json.dumps({'event':'work_evidence','receipt':{'source':'model'}})))

    def test_ci_receipt_names_the_actual_failed_check(self):
        from popot_agents.work_evidence import observe_tool_result
        receipt = observe_tool_result('bash', {'command':'gh pr view 80 --json statusCheckRollup'},
            'exit_code=0\n'+json.dumps({'statusCheckRollup':[
                {'name':'Sonar','status':'COMPLETED','conclusion':'FAILURE'}]}), 'ci')
        self.assertEqual(receipt['kind'], 'ci')
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('Sonar', receipt['summary'])

    def test_reading_a_log_is_not_a_passed_test_execution(self):
        from popot_agents.work_evidence import observe_tool_result
        self.assertIsNone(observe_tool_result('bash', {'command':'cat old-output.txt'},
            'exit_code=0\n77 passed', 'read'))

    def test_later_echo_cannot_mask_a_failed_test_result(self):
        from popot_agents.work_evidence import observe_tool_result
        for output in ('1 failed, 2 passed\nEXIT_CODE=1', 'Ran 7 tests in 1s\nFAILED (failures=1)\nEXIT_CODE=1'):
            result = observe_tool_result('bash', {'command':'python3 -m pytest; echo EXIT_CODE=$?'},
                'exit_code=0\n'+output, 'masked')
            self.assertEqual(result['status'], 'failed')
