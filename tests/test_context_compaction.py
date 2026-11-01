import json
import unittest


class ContextCompactionTests(unittest.TestCase):
    def test_old_outputs_are_saved_but_scope_recent_results_and_uncertainty_survive(self):
        from popot_agents.worker.context import compact_tool_results
        scope = {'role':'user','content':'Never push. Keep authorization unchanged.'}
        messages = [scope, {'role':'system','content':'Role permissions'}]
        for i in range(10):
            messages.extend([{'role':'assistant','tool_calls':[{'id':str(i),
                'function':{'name':'bash','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':str(i),'content':json.dumps({
                    'isError':True,'outcome_unknown':True,'error':'timeout', 'output':'x'*8000})}])
        original = json.loads(json.dumps(messages))
        artifacts = {}
        def save(content):
            path = f'.tool-results/{len(artifacts):032x}.txt'
            artifacts[path] = content
            return path
        compact_tool_results(messages, save, budget_chars=12000, recent_results=2)
        self.assertEqual(messages[:2], original[:2])
        self.assertEqual(messages[-4:], original[-4:])
        compacted = json.loads(messages[3]['content'])
        self.assertTrue(compacted['outcome_unknown'])
        self.assertEqual(compacted['error'], 'timeout')
        self.assertEqual(artifacts[compacted['outputArtifact']], original[3]['content'])
        self.assertIn('not completion', compacted['notice'])
        self.assertLess(sum(len(m.get('content','')) for m in messages), 23000)
        count = len(artifacts)
        compact_tool_results(messages, save, budget_chars=12000, recent_results=2)
        self.assertEqual(len(artifacts), count)

    def test_failed_artifact_write_does_not_discard_original(self):
        from popot_agents.worker.context import compact_tool_results
        messages = [{'role':'tool','tool_call_id':'one','content':'x'*10000}]
        def fail(_): raise OSError('disk full')
        with self.assertRaises(OSError):
            compact_tool_results(messages, fail, budget_chars=1000, recent_results=0)
        self.assertEqual(messages[0]['content'], 'x'*10000)

    def test_plain_shell_failure_keeps_exit_code_and_current_results_are_untouched(self):
        from popot_agents.worker.context import compact_tool_results
        messages = [{'role':'tool','tool_call_id':'failed','content':'exit_code=1\n'+'x'*8000}]
        save = lambda _: '.tool-results/'+'a'*32+'.txt'
        compact_tool_results(messages, save, budget_chars=1000, recent_results=6)
        self.assertTrue(messages[0]['content'].startswith('exit_code=1'))
        compact_tool_results(messages, save, budget_chars=1000, recent_results=0)
        result = json.loads(messages[0]['content'])
        self.assertEqual(result['exit_code'], 1)
        self.assertTrue(result['isError'])
