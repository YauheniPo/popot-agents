"""Durable worker reports remain visible across failures and API restarts."""
import json
import os
import unittest
from unittest.mock import patch


class WorkStatusTests(unittest.TestCase):
    def test_failed_parent_does_not_hide_completed_child_or_export_payloads(self):
        from popot_agents.orchestrator.work_status import build_work_status
        root = {'sessionId': 'a'*16, 'role':'planner', 'updatedAt':'2026-10-31T20:00:00Z',
                'messages':[{'role':'user','content':'private-task'}],
                'roleConfig':{'_workflow':{'state':'blocked', 'phase':'finished', 'outcome':{
                    'status':'blocked', 'evidence':[], 'remaining_work':['reconcile'], 'errors':['MCP failed'],
                    'failure':{'code':'worker_timeout','stage':'waiting_for_children',
                               'error_type':'TimeoutError','childSessionIds':['b'*16]}}}}}
        child = {'sessionId':'b'*16, 'role':'backend_engineer','updatedAt':'2026-10-31T19:59:00Z',
            'roleConfig':{'_ax_delegation':{'root_session_id':'a'*16, 'parent_session_id':'a'*16,
                'state':'TASK_STATE_FAILED', 'input':'private-delegation-input', 'outcome':{
                    'status':'failed', 'reason':'MCP failed',
                    'evidence':['Commit https://github.com/example/team/commit/'+'c'*40,
                                'Workspace CI: success', 'head ' + 'd'*40], 'remaining_work':['QA'], 'errors':['MCP failed'],
                    'last_partial_answer':'private-stdout'}}}}
        foreign = {**child, 'sessionId':'f'*16, 'roleConfig':{'_ax_delegation':{'root_session_id':'e'*16}}}
        result = build_work_status(root, [root, child, foreign])
        self.assertEqual({item['sessionId'] for item in result['tasks']}, {'a'*16,'b'*16})
        receipt = next(item for item in result['tasks'] if item['sessionId']=='b'*16)
        self.assertEqual(receipt['outcome']['evidence'][1], 'Workspace CI: success')
        self.assertEqual(receipt['references'][0]['type'], 'commit')
        self.assertEqual(receipt['references'][0]['sha'], 'c'*40)
        self.assertEqual(receipt['commits'], ['c'*40, 'd'*40])
        self.assertTrue(receipt['reportedAt'])
        self.assertEqual(result['source'], 'stored_worker_reports')
        parent = next(item for item in result['tasks'] if item['sessionId']=='a'*16)
        self.assertEqual(parent['failure']['stage'],'waiting_for_children')
        self.assertEqual(parent['failure']['childSessionIds'],['b'*16])
        self.assertNotIn('private-', json.dumps(result))
        # Reading the same persisted data after a restart needs no live worker.
        self.assertEqual(result, build_work_status(json.loads(json.dumps(root)), [child, root, foreign]))

    def test_reports_are_bounded_redacted_and_truncation_is_explicit(self):
        from popot_agents.orchestrator.work_status import build_work_status
        root = {'sessionId':'a'*16,'role':'planner','updatedAt':'2026-10-31T20:00:00Z','roleConfig':{}}
        children = [{'sessionId':format(i,'016x'), 'role':'backend_engineer', 'updatedAt':str(i),
            'roleConfig':{'_ax_delegation':{'root_session_id':'a'*16,'parent_session_id':'a'*16,
                'state':'TASK_STATE_COMPLETED','outcome':{'status':'completed',
                    'evidence':['test-secret-value ' + 'я'*10000]*30,'errors':[], 'remaining_work':[]}}}}
            for i in range(20)]
        with patch.dict(os.environ, {'REPORT_TEST_API_KEY':'test-secret-value'}):
            result = build_work_status(root, [root,*children])
            focused = build_work_status(children[-1], [root,*children])
            self.assertEqual(focused['tasks'][0]['sessionId'], children[-1]['sessionId'])
        encoded = json.dumps(result,ensure_ascii=False).encode()
        self.assertLessEqual(len(encoded), 16384)
        self.assertNotIn('test-secret-value', encoded.decode())
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['tasks']), 8)
