"""Opt-in workspace isolation and DATA-resume checks in a real AX sandbox."""

import base64
import json
import os
from pathlib import Path
import unittest
import uuid

from ax_local.api.ax_runner import AxAgentRunner


class PermissionsRunner(AxAgentRunner):
    def task_manifest(self, chat_id, role_config=None):
        manifest = super().task_manifest(chat_id, role_config)
        script = r'''
import json, os, stat, subprocess, sys
from pathlib import Path
import popot_agents.worker.session_worker as worker
from ax_local.worker.entrypoint import main
from popot_agents.tools import _file_action
from popot_agents.worker.mcp_client import discover_tools

def answer(*args, **kwargs):
    info = os.stat('/workspace')
    assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (10001, 10001, 0o700)
    marker = Path('/workspace/permission-marker')
    previous = marker.read_text() if marker.exists() else 'fresh'
    _file_action('write', {'path': 'permission-marker', 'content': 'kept'})
    assert 'kept' in _file_action('read', {'path': 'permission-marker'})
    child = subprocess.run([sys.executable, '-c', """
import os
from pathlib import Path
assert os.getuid() == 10001
caps = next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))
assert int(caps, 16) == 0, caps
assert Path('/workspace/permission-marker').read_text() == 'kept'
try:
    os.unlink('/run/popot/chat.sock')
except PermissionError:
    pass
else:
    raise AssertionError('tool user could replace the control socket')
"""], user=10001, group=10001, extra_groups=[], capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
    stranger = subprocess.run([sys.executable, '-c', """
from pathlib import Path
for operation in (lambda: Path('/workspace/permission-marker').read_text(),
                  lambda: Path('/workspace/stranger').write_text('forbidden')):
    try:
        operation()
    except PermissionError:
        pass
    else:
        raise AssertionError('unrelated UID accessed the workspace')
"""], user=10002, group=10002, extra_groups=[], capture_output=True, text=True)
    assert stranger.returncode == 0, stranger.stderr
    adapter = '/app/popot_agents/worker/cognee_memory.py'
    tools = discover_tools({'memory': {'command': [sys.executable, adapter],
                                      'tools': ['recall']}}, timeout_seconds=10)
    assert 'mcp__memory__recall' in tools
    return json.dumps({'previous': previous, 'tool_uid': 10001, 'capabilities': 0,
                       'workspace_mode': '0700', 'other_uid_denied': True,
                       'control_socket_protected': True, 'mcp': 'ok'})
worker.run_http = answer
main()
'''
        encoded = base64.b64encode(script.encode()).decode()
        manifest['spec']['command'] = ['python', '-u', '-c',
                                      'import base64; exec(base64.b64decode(' + repr(encoded) + '))']
        return manifest


@unittest.skipUnless(os.getenv('AX_TEST_WORKSPACE_PERMISSIONS_LIVE'),
                     'requires local AX and the updated controller/worker image')
class WorkspacePermissionsLiveTests(unittest.TestCase):
    def test_tool_access_is_private_and_survives_data_resume(self):
        image = os.getenv('AX_TEST_WORKER_IMAGE') or Path(
            os.environ['AX_LOCAL_WORKER_IMAGE_FILE']).read_text().strip()
        runner = PermissionsRunner(image=image, model='unused', base_url='http://localhost:1/v1')
        runner.snapshot_mode = 'data'
        chat = None
        try:
            chat = runner.start_chat(uuid.uuid4().hex[:16], {'tools': ['read_file', 'write_file'],
                                                          'timeout_seconds': 60})
            for previous in ('fresh', 'kept'):
                result = json.loads(chat.send('check permissions')['answer'])
                self.assertEqual(result, {'previous': previous, 'tool_uid': 10001,
                                         'capabilities': 0, 'workspace_mode': '0700',
                                         'other_uid_denied': True,
                                         'control_socket_protected': True, 'mcp': 'ok'})
                chat.after_turn()
                self.assertEqual(chat.status, 'suspended')
            print('AX workspace 0700: file/MCP tools work; other UID and socket access denied; DATA resume passed')
        finally:
            if chat:
                chat.close()
            runner.shutdown_worker_queue()
            runner.maintenance()


if __name__ == '__main__':
    unittest.main()
