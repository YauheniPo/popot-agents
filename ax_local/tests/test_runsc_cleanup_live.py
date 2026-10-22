"""Opt-in reproduction of partial runsc deletion during local API startup."""

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from ax_local.api.ax_runner import AxAgentRunner
from ax_local.config import AX_CONFIG


class CleanupFixtureRunner(AxAgentRunner):
    def task_manifest(self, chat_id, role_config=None):
        manifest = super().task_manifest(chat_id, role_config)
        script = '''
import popot_agents.worker.session_worker as worker
worker.run_http = lambda *args, **kwargs: 'cleanup fixture'
from ax_local.worker.entrypoint import main
main()
'''
        encoded = base64.b64encode(script.encode()).decode()
        manifest['spec']['command'] = ['python', '-u', '-c',
            "import base64; exec(base64.b64decode('" + encoded + "'))"]
        return manifest


@unittest.skipUnless(os.getenv('AX_TEST_RUNSC_CLEANUP_LIVE'), 'requires local AX; restarts the local API')
class LiveRunscCleanupTests(unittest.TestCase):
    def test_api_recovers_missing_guest_and_pause_filestore_without_manual_cleanup(self):
        root = Path(__file__).resolve().parents[2]
        kubeconfig = root / 'ax_local/.local/kubeconfig'
        kube = ['kubectl', '--kubeconfig=' + str(kubeconfig), '--context=' + AX_CONFIG['context']]
        ate = [str(root / 'ax_local/.local/bin/kubectl-ate'), *kube[1:]]
        api = 'popot-ax-local-api-1'

        def run(args, **kwargs):
            return subprocess.run(args, capture_output=True, text=True, check=True, timeout=45, **kwargs).stdout

        environment = {'PATH': str(root / 'ax_local/.local/bin') + os.pathsep + os.environ['PATH'],
                       'KUBECONFIG': str(kubeconfig)}
        with patch.dict(os.environ, environment), tempfile.TemporaryDirectory() as temporary:
            runner = CleanupFixtureRunner(image=(root / 'ax_local/.local/worker-image').read_text().strip(),
                                           model='unused', base_url='http://localhost:1/v1')
            existing = runner._run('get', 'tasks').stdout.splitlines()[1:]
            self.assertFalse(any(line.strip() for line in existing), 'finish other AX Tasks before this startup test')
            chat, helper_path = None, None
            try:
                session_id = uuid.uuid4().hex[:16]
                chat = runner.start_chat(session_id, {'tools': [], 'instructions': 'cleanup fixture', 'env_names': []})
                actors = json.loads(run([*ate, 'get', 'actors', '--atespace=' + AX_CONFIG['atespace'], '-o', 'json']))['actors']
                actor = next(item for item in actors if item['metadata']['name'] == chat.container_name)
                actor_uid = str(uuid.UUID(actor['metadata']['uid']))
                assignment = actor['status']['workerAssignment']
                worker_namespace, pod = assignment['workerNamespace'], assignment['workerPod']
                arch = run(['go', 'env', 'GOARCH']).strip()
                configs = json.loads(run([*kube, '-n', AX_CONFIG['atespace'], 'get', 'sandboxconfigs', '-o', 'json']))['items']
                config = next(item for item in configs if item['spec']['sandboxClass'] == 'gvisor')
                digest = config['spec']['assets'][arch]['gvisor']['sha256']
                runtime = [*kube, '-n', worker_namespace, 'exec', pod, '--',
                    f'/var/lib/ateom-gvisor/static-files/gvisor-{digest}/runsc',
                    '-root', f'/var/lib/ateom-gvisor/actors/{actor_uid}/runsc-state']
                run([*runtime, 'kill', 'guest', 'SIGKILL'])
                run([*runtime, 'kill', '_pause', 'SIGKILL'])
                self.assertEqual(json.loads(run([*runtime, 'state', '_pause']))['status'], 'stopped')
                run([*runtime, 'delete', '-force', 'guest'])
                self.assertNotIn('guest', run([*runtime, 'list', '-quiet']).split())

                # Distroless workers have no shell. This temporary static fixture
                # can remove only the stopped test actor's pause filestore.
                source = Path(temporary) / 'fixture.go'
                source.write_text('''package main
import ("fmt"; "os"; "path/filepath"; "regexp")
func main() {
 if len(os.Args) != 2 || !regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`).MatchString(os.Args[1]) { os.Exit(2) }
 path := filepath.Join("/var/lib/ateom-gvisor/actors", os.Args[1], "bundles/_pause/rootfs/.gvisor.filestore._pause")
 if err := os.Remove(path); err != nil && !os.IsNotExist(err) { panic(err) }
 fmt.Println("fixture pause filestore absent")
}
''')
                binary = Path(temporary) / 'fixture'
                run(['go', 'build', '-o', str(binary), str(source)],
                    env={**os.environ, 'GOOS': 'linux', 'GOARCH': arch, 'CGO_ENABLED': '0'})
                helper_path = '/var/lib/ateom-gvisor/cleanup-fixture-' + session_id
                node = 'popot-ax-control-plane'
                run(['docker', 'cp', str(binary), node + ':' + helper_path])
                run(['docker', 'exec', node, 'chmod', '500', helper_path])
                self.assertIn('filestore absent', run([*kube, '-n', worker_namespace, 'exec', pod,
                                                       '--', helper_path, actor_uid]))
                print('Fixture: guest state removed and stopped pause filestore absent', flush=True)

                started = time.monotonic()
                run(['docker', 'restart', api])
                started_at = run(['docker', 'inspect', api, '--format', '{{.State.StartedAt}}']).strip()
                while time.monotonic() - started < 60:
                    health = run(['docker', 'inspect', api, '--format', '{{.State.Health.Status}}']).strip()
                    if health == 'healthy':
                        break
                    time.sleep(0.5)
                self.assertEqual(health, 'healthy')
                self.assertEqual(run(['docker', 'inspect', api, '--format', '{{.State.StartedAt}}']).strip(), started_at)
                self.assertIsNone(runner.task_status(chat.container_name)[0])
                print(f'API recovered partial sandbox deletion in {time.monotonic() - started:.1f}s', flush=True)
            finally:
                if chat:
                    chat.close()
                if helper_path:
                    run(['docker', 'exec', 'popot-ax-control-plane', 'rm', '-f', helper_path])
                runner.shutdown_worker_queue()
                runner.maintenance()


if __name__ == '__main__':
    unittest.main()
