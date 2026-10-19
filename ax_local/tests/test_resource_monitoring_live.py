"""Opt-in resource collection check against gVisor, with no model calls."""

import base64
import json
import os
from pathlib import Path
import time
import unittest
from urllib.parse import urlencode
from urllib.request import urlopen
from unittest.mock import patch
import uuid

from ax_local.api.ax_runner import AxAgentRunner


@unittest.skipUnless(os.getenv('AX_TEST_LIVE'), 'requires local AX monitoring and worker image')
class LiveResourceMonitoringTests(unittest.TestCase):
    def test_child_process_cpu_and_rss_reach_prometheus(self):
        root = Path(__file__).resolve().parents[2]
        environment = {'PATH': str(root / 'ax_local/.local/bin') + os.pathsep + os.environ['PATH'],
                       'KUBECONFIG': str(root / 'ax_local/.local/kubeconfig')}
        with patch.dict(os.environ, environment):
            runner = AxAgentRunner(image=(root / 'ax_local/.local/worker-image').read_text().strip(),
                                   model='unused', base_url='http://localhost:1/v1')
            manifest = runner.task_manifest(uuid.uuid4().hex[:16], {'_ax_role': 'backend_engineer'})
            name = manifest['metadata']['name']
            script = '''
import subprocess, time
from ax_local.worker.process_metrics import ProcessMetrics
child = subprocess.Popen(['python', '-c', 'import time; data=bytearray(16*1024*1024); exec("while True: sum(range(20000)); time.sleep(0.002)")'])
try:
    with ProcessMetrics.from_env():
        time.sleep(150)
finally:
    child.terminate()
    child.wait()
'''
            encoded = base64.b64encode(script.encode()).decode()
            manifest['spec']['command'] = ['python', '-u', '-c',
                "import base64; exec(base64.b64decode('" + encoded + "'))"]

            def query(expression):
                url = ('http://127.0.0.1:3002/api/datasources/proxy/uid/popot-prometheus/api/v1/query?'
                       + urlencode({'query': expression}))
                with urlopen(url, timeout=15) as response:
                    data = json.load(response)
                self.assertEqual(data['status'], 'success', data)
                return data['data']['result']

            try:
                runner._run('apply', '-f', '-', input=json.dumps(manifest))
                deadline = time.monotonic() + 110
                rows = []
                while time.monotonic() < deadline:
                    rows = query(f'popot_sandbox_process_memory_bytes{{task="{name}"}}')
                    if len(rows) >= 2:
                        break
                    time.sleep(3)
                self.assertGreaterEqual(len(rows), 2, 'no individual gVisor processes reached Prometheus')
                self.assertTrue(all(r['metric'].get('role') == 'backend_engineer' for r in rows), rows)
                self.assertEqual(query(f'popot_sandbox_process_memory_bytes{{task="{name}",role="other_role"}}'), [])
                self.assertGreater(max(float(r['value'][1]) for r in rows), 16 * 1024 * 1024)
                rates = []
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    rates = query(f'rate(popot_sandbox_process_cpu_seconds_total{{task="{name}"}}[1m])')
                    if any(float(r['value'][1]) > 0 for r in rates):
                        break
                    time.sleep(3)
                self.assertTrue(any(float(r['value'][1]) > 0 for r in rates), rates)
                runner._run('suspend', 'task', name)
                deadline = time.monotonic() + 45
                while runner.task_status(name)[0] != 'Suspended' and time.monotonic() < deadline:
                    time.sleep(2)
                self.assertEqual(runner.task_status(name)[0], 'Suspended')
                active = f'time() - popot_sandbox_process_sampled_at_seconds{{task="{name}"}} < 20'
                deadline = time.monotonic() + 35
                while query(active) and time.monotonic() < deadline:
                    time.sleep(3)
                self.assertEqual(query(active), [], 'suspended sandbox still appears active')
                history = query(f'count by(role)(count by(role,task)(max_over_time('
                                f'popot_sandbox_process_sampled_at_seconds{{task="{name}",role="backend_engineer"}}[5m])))')
                self.assertEqual(len(history), 1, 'role history disappeared after suspend')
                self.assertEqual(history[0]['metric']['role'], 'backend_engineer')
                self.assertEqual(float(history[0]['value'][1]), 1)
                # Raw cached samples remain available for history after suspend.
                self.assertGreaterEqual(len(query(f'popot_sandbox_process_memory_bytes{{task="{name}"}}')), 2)
                self.assertEqual(runner.task_status(name)[0], 'Suspended', 'monitoring resumed the task')
                print(f'AX process metrics: {len(rows)} PIDs, role filtering, CPU/RSS and role history after suspend confirmed')
            finally:
                runner._run('delete', 'task', name, allow_failure=True)


if __name__ == '__main__':
    unittest.main()
