"""Check the deployed Cognee importer against the current local checkout."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('docker') and os.getenv('COGNEE_TEST_MOUNTS') == '1',
                     'requires the local Cognee container and COGNEE_TEST_MOUNTS=1')
class CogneeMountTests(unittest.TestCase):
    def test_importer_loads_current_runtime_sources(self):
        result = subprocess.run(['docker', 'exec', 'popot-memory-cognee-1', 'python',
            '/app/popot-sync/import_skills.py', '--help'],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = {'popot_agents/runtime_config.py': '/app/popot_agents/runtime_config.py',
                 'config/runtime.json': '/app/config/runtime.json',
                 'memory/import_skills.py': '/app/popot-sync/import_skills.py'}
        code = ('from pathlib import Path; import hashlib,json; print(json.dumps('
                '{name:hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in '
                + repr(paths) + '.items()}))')
        readback = subprocess.run(['docker', 'exec', 'popot-memory-cognee-1', 'python', '-c', code],
            check=True, capture_output=True, text=True, timeout=15)
        expected = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}
        self.assertEqual(json.loads(readback.stdout), expected)
