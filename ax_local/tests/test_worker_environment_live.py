"""Opt-in real AX checks of unprivileged package installation, Ansible, multiprocessing and rsync, without LLM/MCP calls."""
import json
import os
import unittest
import uuid
from pathlib import Path

from ax_local.api.ax_runner import AxAgentRunner


PROBE = r'''
import json
from popot_agents.tools import execute_tool
script = r"""
import json, multiprocessing, os, pwd, shutil, subprocess, tempfile
from pathlib import Path
assert os.geteuid() == 10001, os.geteuid()
for tool in ('cc', 'make', 'pkg-config', 'rg', 'unzip', 'git', 'node', 'npm'):
    assert shutil.which(tool), 'missing development tool: '+tool
print(json.dumps({'home':os.environ['HOME'], 'passwd_home':pwd.getpwuid(os.geteuid()).pw_dir}), flush=True)
assert pwd.getpwuid(os.geteuid()).pw_dir == os.environ['HOME']
shm = os.stat('/dev/shm')
assert shm.st_mode & 0o7777 == 0o700
assert shm.st_uid == os.geteuid()
ctx = multiprocessing.get_context('fork')
with ctx.Lock():
    queue = ctx.Queue()
    child = ctx.Process(target=queue.put, args=('multiprocessing-ok',))
    child.start()
    try:
        assert queue.get(timeout=10) == 'multiprocessing-ok'
        child.join(timeout=10)
        assert child.exitcode == 0, child.exitcode
    finally:
        if child.is_alive():
            child.kill()
        child.join()
        queue.close()
        queue.join_thread()
with tempfile.TemporaryDirectory() as root:
    source, target = Path(root)/'source', Path(root)/'target'
    source.mkdir()
    native = Path(root)/'native'
    native.with_suffix('.c').write_text('int main(void) { return 0; }')
    subprocess.run(['make', str(native)], check=True, timeout=15)
    subprocess.run([str(native)], check=True, timeout=5)
    package = Path(root)/'npm-package'
    package.mkdir()
    (package/'package.json').write_text(json.dumps({'name':'worker-environment-probe',
        'version':'1.0.0', 'bin':{'worker-environment-probe':'cli.js'}}))
    (package/'cli.js').write_text('#!/usr/bin/env node\nconsole.log("npm-ok")\n')
    subprocess.run(['npm', 'install', '--global', '--offline', '--ignore-scripts',
                    '--no-audit', '--no-fund', str(package)], check=True, timeout=30)
    try:
        assert subprocess.check_output(['worker-environment-probe'], text=True,
                                       timeout=10).strip() == 'npm-ok'
    finally:
        subprocess.run(['npm', 'uninstall', '--global', '--offline', '--ignore-scripts',
                        'worker-environment-probe'], check=True, timeout=30)
    (source/'sample.txt').write_text('rsync-ok')
    subprocess.run(['rsync', '-a', str(source)+'/', str(target)+'/'], check=True, timeout=10)
    assert (target/'sample.txt').read_text() == 'rsync-ok'
# Exercise installation and a real local target without overriding remote_tmp.
with tempfile.TemporaryDirectory(dir=os.environ['HOME']) as root:
    root = Path(root)
    venv = root/'venv'
    subprocess.run(['python', '-m', 'venv', str(venv)], check=True, timeout=30)
    subprocess.run([str(venv/'bin/pip'), 'install', '--disable-pip-version-check',
                    'ansible-core'], check=True, timeout=90, stdout=subprocess.PIPE)
    play = root/'play.json'
    play.write_text(json.dumps([{'hosts':'localhost', 'gather_facts':False,
        'tasks':[{'ansible.builtin.command':{'argv':['python', '-c',
            "import os, pathlib; p=pathlib.Path(os.path.expanduser('~agent'))/'.worker-home-probe'; p.write_text('ok'); p.unlink(); print('ansible-ok')"]},
            'register':'probe'}, {'ansible.builtin.assert':{'that':[
                "probe.stdout == 'ansible-ok'"]}}]}]))
    subprocess.run(['python', '-m', 'pip', 'install', '--user',
                    '--disable-pip-version-check', 'ansible-core'], check=True, timeout=90,
                    stdout=subprocess.PIPE)
    subprocess.run(['ansible-playbook', '--version'], check=True, timeout=10,
                    stdout=subprocess.PIPE)
    subprocess.run([str(venv/'bin/ansible-playbook'), '-i', 'localhost,', '-c', 'local',
                    str(play)], check=True, timeout=30)
print(json.dumps({'uid':os.geteuid(),'multiprocessing':'passed','rsync':'passed',
                  'venv_install':'passed','npm_install':'passed','native_build':'passed',
                  'ansible':'passed'}))
"""
execute_tool('write_file', {'path':'worker_environment_probe.py', 'content':script},
             ['write_file'], timeout_seconds=10)
result = execute_tool('bash', {'command':'python worker_environment_probe.py'},
                      ['bash'], timeout_seconds=180)
print(result)
assert result.startswith('exit_code=0\n'), result
'''


@unittest.skipUnless(os.getenv('AX_TEST_ENVIRONMENT_LIVE'), 'requires local API container and AX worker image')
class LiveWorkerEnvironmentTests(unittest.TestCase):
    def test_development_environment_survives_snapshot_restore(self):
        image = Path(os.environ['AX_LOCAL_WORKER_IMAGE_FILE']).read_text().strip()
        runner = AxAgentRunner(image, 'diagnostic-no-llm', 'http://127.0.0.1:1/v1')
        chat = runner.start_chat(uuid.uuid4().hex[:16], {'tools': ['bash']})
        try:
            for phase in ('fresh', 'restored'):
                with self.subTest(phase=phase):
                    if phase == 'restored':
                        chat.after_turn()
                        self.assertTrue(chat.suspended)
                        chat.resume()
                    result = runner._run('ssh', chat.container_name, '--', 'python', '-c', PROBE,
                                         timeout=210, allow_failure=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    for capability in ('multiprocessing', 'rsync', 'ansible', 'npm_install', 'native_build'):
                        self.assertIn('"'+capability+'": "passed"', result.stdout)
                    print(json.dumps({'image': image, 'phase': phase,
                                      'unprivileged_worker_environment': 'passed'}), flush=True)
        finally:
            chat.close()


if __name__ == '__main__':
    unittest.main()
