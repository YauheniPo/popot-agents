"""Opt-in lifecycle check against local AX/Substrate, without any model calls."""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from ax_local.api.ax_runner import AxAgentRunner
from ax_local.api import ax_runner
from ax_local.config import AX_CONFIG
from popot_agents.orchestrator.main import create_server
from tests import test_chat as chat_tests


class LifecycleRunner(AxAgentRunner):
    def task_manifest(self, chat_id, role_config=None):
        manifest = super().task_manifest(chat_id, role_config)
        # Use the production conversation server with a deterministic answer.
        # A workspace marker distinguishes snapshot restore from a new worker.
        script = '''
from pathlib import Path
import popot_agents.worker.session_worker as worker
from ax_local.worker.entrypoint import main
def answer(messages, *args, **kwargs):
    marker = Path('/workspace/lifecycle-marker')
    previous = marker.read_text() if marker.exists() else 'fresh'
    marker.write_text('preserved')
    artifact = Path('/workspace/lifecycle-artifact.bin')
    if not artifact.exists():
        artifact.write_bytes(bytes(range(256)) * 4096)
    return previous + '|' + '|'.join(m['content'] for m in messages if m['role'] == 'user')
worker.run_http = answer
main()
'''
        # Keep multiline code out of the Task's YAML command serialization.
        encoded = base64.b64encode(script.encode()).decode()
        manifest['spec']['command'] = [
            'python', '-u', '-c', "import base64; exec(base64.b64decode('" + encoded + "'))"]
        return manifest


@unittest.skipUnless(os.getenv('AX_TEST_LIVE'), 'requires local AX/Substrate and worker image')
class LiveSessionLifecycleTests(unittest.TestCase):
    def test_unconfirmed_suspend_waits_for_native_snapshot_before_continuing(self):
        root = Path(__file__).resolve().parents[2]
        image = os.getenv('AX_TEST_WORKER_IMAGE') or (
            root / 'ax_local/.local/worker-image').read_text().strip()
        environment = {'PATH': str(root / 'ax_local/.local/bin') + os.pathsep + os.environ['PATH'],
                       'KUBECONFIG': str(root / 'ax_local/.local/kubeconfig')}
        with patch.dict(os.environ, environment), patch.dict(AX_CONFIG, {'snapshot_mode': 'data'}):
            runner = LifecycleRunner(image=image, model='unused', base_url='http://localhost:1/v1')
            chat = runner.start_chat(uuid.uuid4().hex[:16])
            try:
                self.assertEqual(chat.send('remember 137')['answer'], 'fresh|remember 137')
                command = runner._run
                def lose_suspend_reply(*args, **kwargs):
                    result = command(*args, **kwargs)
                    if args[0] == 'suspend':
                        raise subprocess.TimeoutExpired(['ax', 'suspend'], .01)
                    return result
                with patch.object(runner, '_run', side_effect=lose_suspend_reply):
                    chat.after_turn()
                self.assertEqual(chat.status, 'snapshot_unconfirmed')
                self.assertEqual(chat.send('continue')['answer'], 'preserved|remember 137|continue')
                self.assertEqual(chat.status, 'running')
                result = runner._run('ssh', chat.container_name, '--', 'sha256sum',
                                    '/workspace/lifecycle-artifact.bin')
                self.assertIn(hashlib.sha256(bytes(range(256)) * 4096).hexdigest(), result.stdout)
            finally:
                chat.close()
                runner.shutdown_worker_queue()
                runner.maintenance()
                # AX retains golden actors after removing a custom Task. This
                # fixture's unique template cannot belong to another session.
                actors = subprocess.run(['kubectl-ate', '--context=' + AX_CONFIG['context'],
                    'get', 'actors', '-A', '-o', 'json'], capture_output=True,
                    text=True, check=True, timeout=30)
                prefix = chat.container_name + '-tmpl-'
                for actor in json.loads(actors.stdout)['actors']:
                    template = actor.get('actorTemplate', {})
                    if (template.get('atespace') == AX_CONFIG['atespace']
                            and template.get('name', '').startswith(prefix)):
                        metadata = actor['metadata']
                        subprocess.run(['kubectl-ate', '--context=' + AX_CONFIG['context'],
                            'delete', 'actor', metadata['name'], '-a', metadata['atespace'],
                            '--any-state'], capture_output=True, text=True, check=True, timeout=30)

    def test_suspend_continuation_and_expiry_restore_context(self):
        root = Path(__file__).resolve().parents[2]
        image = os.getenv('AX_TEST_WORKER_IMAGE') or (
            root / 'ax_local/.local/worker-image').read_text().strip()
        environment = {'PATH': str(root / 'ax_local/.local/bin') + os.pathsep + os.environ['PATH'],
                       'KUBECONFIG': str(root / 'ax_local/.local/kubeconfig')}
        mode = os.getenv('AX_TEST_SNAPSHOT_MODE', AX_CONFIG['snapshot_mode'])
        self.assertIn(mode, ('data', 'full'))
        with patch.dict(os.environ, environment), patch.dict(AX_CONFIG, {'snapshot_mode': mode}), \
             tempfile.TemporaryDirectory() as directory:
            runner = LifecycleRunner(image=image, model='unused', base_url='http://localhost:1/v1')
            server = create_server({'test': runner}, port=0, session_dir=directory)
            try:
                def post(payload):
                    return chat_tests.TaskSessionTests._post_to_handler(
                        server.RequestHandlerClass, server, payload)

                status, first = post({'agent': 'test', 'message': 'remember 137'})
                self.assertEqual(status, 200, first)
                self.assertEqual(first['answer'], 'fresh|remember 137')
                sid = first['sessionId']
                original = server.live[sid].worker
                self.assertEqual(original.status, 'suspended')
                self.assertEqual(runner.task_status(original.container_name)[0], 'Suspended')

                expected = 'remember 137'
                for turn in range(3):
                    message = 'continue ' + str(turn)
                    expected += '|' + message
                    with patch.object(ax_runner, 'emit_progress', wraps=ax_runner.emit_progress) as progress:
                        status, second = post({'sessionId': sid, 'message': message})
                    self.assertEqual(status, 200, second)
                    resumed = server.live[sid].worker
                    self.assertIs(resumed, original, 'runtime could not restore the native snapshot')
                    self.assertEqual(second['answer'], 'preserved|' + expected)
                    self.assertEqual(resumed.status, 'suspended')
                    events = [event for event in progress.call_args_list
                              if event.args[0] in ('snapshot_saved', 'snapshot_restored')]
                    self.assertEqual([event.args[0] for event in events],
                                     ['snapshot_restored', 'snapshot_saved'])
                    for event in events:
                        self.assertEqual(event.kwargs['mode'], mode)
                        print('AX live snapshot:', event.args[0], event.kwargs)
                # Check binary files too; a transcript-only fallback cannot pass.
                resumed.resume()
                result = runner._run('ssh', resumed.container_name, '--', 'sha256sum',
                                     '/workspace/lifecycle-artifact.bin')
                self.assertIn(hashlib.sha256(bytes(range(256)) * 4096).hexdigest(), result.stdout)
                # Exercise real chunk transport beyond its former 200 KB limit.
                committed = list(resumed._messages)
                resumed.restore([{'role': 'user', 'content': 'я' * 190000},
                                 {'role': 'assistant', 'content': 'готово'}])
                resumed.restore(committed)
                resumed.after_turn()

                # Advance beyond the worker's configured retention without waiting.
                server.live[sid].last_used = time.monotonic() - resumed.idle_timeout_seconds - 1
                server.service_actions()
                self.assertTrue(resumed.closed)
                self.assertNotIn(sid, server.live)
                self.assertIsNotNone(server.store.get(sid))

                status, third = post({'sessionId': sid, 'message': 'after expiry'})
                self.assertEqual(status, 200, third)
                self.assertIsNot(server.live[sid].worker, resumed)
                self.assertEqual(third['answer'], 'fresh|' + expected + '|after expiry')
                self.assertEqual(server.live[sid].worker.status, 'suspended')
            finally:
                server.server_close()


if __name__ == '__main__':
    unittest.main()
