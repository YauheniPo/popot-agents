"""Memory settings have one source; shared evidence limits remain runtime settings."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MemoryConfigTests(unittest.TestCase):
    def test_memory_settings_are_separate_from_shared_evidence_limits(self):
        runtime = json.loads((ROOT / 'config/runtime.json').read_text())
        memory = json.loads((ROOT / 'memory/config.json').read_text())['x-agent']
        self.assertNotIn('memory', runtime)
        self.assertFalse(any(key.startswith('memory_') for key in runtime['timeouts']))
        for key in ('tool_evidence_chars', 'tool_evidence_count'):
            self.assertGreater(runtime['limits'][key], 0)
            self.assertNotIn(key, memory)

    def test_custom_memory_settings_are_validated_without_service_configuration(self):
        from popot_agents.runtime_config import load_memory_config

        settings = json.loads((ROOT / 'memory/config.json').read_text())['x-agent']
        settings['recall_top_k'] = 3
        settings['timeouts']['memory_request_seconds'] = 2.5
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'memory.json'
            path.write_text(json.dumps({'x-agent': settings}))
            self.assertEqual(load_memory_config(path), settings)
            for key, value in [('summary_attempts', 0), ('summary_max_tokens', True),
                               ('recall_top_k', 21), ('max_memories', 6),
                               ('trace_tool_limit', 201), ('unexpected', 1)]:
                with self.subTest(key=key):
                    path.write_text(json.dumps({'x-agent': {**settings, key: value}}))
                    with self.assertRaises(ValueError):
                        load_memory_config(path)
            for key, value in [('memory_summary_seconds', float('inf')),
                               ('memory_request_seconds', 0),
                               ('memory_capture_margin_seconds', settings['timeouts']['memory_capture_min_seconds'])]:
                with self.subTest(timeout=key):
                    changed = {**settings, 'timeouts': {**settings['timeouts'], key: value}}
                    path.write_text(json.dumps({'x-agent': changed}))
                    with self.assertRaises(ValueError):
                        load_memory_config(path)
            del settings['summary_attempts']
            path.write_text(json.dumps({'x-agent': settings}))
            with self.assertRaises(ValueError):
                load_memory_config(path)

    def test_custom_files_reach_memory_client_worker_and_shared_result_evidence(self):
        runtime = json.loads((ROOT / 'config/runtime.json').read_text())
        runtime['limits'].update(tool_evidence_count=1, tool_evidence_chars=80)
        config = json.loads((ROOT / 'memory/config.json').read_text())
        config['x-agent'].update(task_chars=17, recall_top_k=3)
        config['x-agent']['timeouts']['memory_tool_request_seconds'] = 2.5
        with tempfile.TemporaryDirectory() as tmp:
            runtime_path, memory_path = Path(tmp) / 'runtime.json', Path(tmp) / 'memory.json'
            runtime_path.write_text(json.dumps(runtime))
            memory_path.write_text(json.dumps(config))
            code = '''
import json
from unittest.mock import Mock, patch
from popot_agents.worker.cognee_memory import CogneeMemory
from popot_agents.worker.turn_memory import TurnMemory
from popot_agents.worker.final_result import retained_receipts
client = Mock()
client.recall.return_value = []
with patch('popot_agents.worker.turn_memory.safe_tool_env', return_value={'COGNEE_PASSWORD': 'test'}):
    memory = TurnMemory({'mcpServers': {'cognee': {}}, 'memory_project': 'config-test'},
                        [{'role': 'user', 'content': 'x' * 40}], client=client)
    memory.context(2)
children = {str(i): {'role': 'qa', 'sessionId': str(i), 'answer': 'done'} for i in range(2)}
print(json.dumps([len(memory.task), client.recall.call_args.kwargs['top_k'],
                  CogneeMemory('http://localhost:3001', 'test').timeout,
                  len(retained_receipts([], children))]))
'''
            result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                                    env={**os.environ, 'POPOT_RUNTIME_CONFIG': str(runtime_path),
                                         'POPOT_MEMORY_CONFIG': str(memory_path)},
                                    text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [17, 3, 2.5, 1])

