"""Role-local MCP entrypoint for the versioned Phoenix runtime evaluator."""
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Literal
import uuid


if not __package__:
    # Avoid placing evals/phoenix.py ahead of installed packages.
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from evals import exclusive, save
from evals.runtime import API, VERSION, annotate, build_report, fetch_spans, render, timestamp


class BoundedAPI(API):
    def __init__(self, endpoint, key, *, max_spans=20000):
        super().__init__(endpoint, key)
        self.deadline = time.monotonic() + 90
        self.max_spans, self.seen = max_spans, 0

    def call(self, path, *, params=None, payload=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Runtime eval deadline exceeded; narrow the time window')
        self.timeout_seconds = min(30, remaining)
        page = super().call(path, params=params, payload=payload)
        if payload is None:
            self.seen += len(page['data'])
            if self.seen > self.max_spans:
                raise ValueError('Runtime eval span limit exceeded; narrow the time window')
        return page


class RuntimeEvals:
    def __init__(self, workspace='/workspace'):
        self.workspace = Path(workspace).resolve()

    def directory(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
            raise ValueError('run_id must be the ID returned by run_runtime_evals')
        directory = (self.workspace / '.runtime-evals' / run_id).resolve()
        if not directory.is_relative_to(self.workspace):
            raise ValueError('Eval directory must be inside the workspace')
        return directory

    def run(self, hours=12, end=None, publish=True):
        if type(hours) not in (int, float) or not math.isfinite(hours) or not 0 < hours <= 168:
            raise ValueError('hours must be between 0 and 168')
        if type(publish) is not bool:
            raise ValueError('publish must be boolean')
        stop = timestamp(end) if end else datetime.now(timezone.utc)
        start = stop - timedelta(hours=hours)
        key = os.environ.get('PHOENIX_API_KEY')
        if not key:
            raise ValueError('PHOENIX_API_KEY is required in the role environment')
        config = json.loads(Path(__file__).with_name('config.json').read_text())['x-tracing']
        provenance = {'project': config['project'], 'endpoint': config['endpoint'],
                      'start_time': start.isoformat(), 'end_time': stop.isoformat(),
                      'evaluator_sha256': hashlib.sha256(Path(__file__).with_name('runtime.py').read_bytes()).hexdigest()}
        run_id = (hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()[:32]
                  if end else uuid.uuid4().hex)
        directory = self.directory(run_id)
        api = BoundedAPI(config['endpoint'], key)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive(directory / '.lock'):
            return self._capture(directory, provenance, api, publish)

    def _capture(self, directory, provenance, api, publish):
        run_id = directory.name
        reused = (directory / 'snapshot.json').is_file()
        if reused:
            snapshot = json.loads((directory / 'snapshot.json').read_text())
            provenance, spans = snapshot['provenance'], snapshot['spans']
        else:
            spans = fetch_spans(api, provenance['project'], provenance['start_time'], provenance['end_time'])
            provenance['captured_at'] = datetime.now(timezone.utc).isoformat()
            save(directory / 'snapshot.json', {'provenance': provenance, 'spans': spans})
        provenance['snapshot_sha256'] = hashlib.sha256((directory / 'snapshot.json').read_bytes()).hexdigest()
        report = build_report(spans, provenance)
        save(directory / 'report.json', report)
        summary = render(report)
        (directory / 'summary.md').write_text(summary)
        receipt_path = directory / 'publication.json'
        receipt = (json.loads(receipt_path.read_text()) if receipt_path.is_file() else
                   {'run_id': run_id, 'published': False, 'annotations': 0})
        save(directory / 'publication.json', receipt)
        # Keep large snapshots out of native artifact handoff. Compressed JSON
        # preserves the full report within the existing binary artifact budget.
        artifacts = (self.workspace / 'artifacts').resolve()
        if not artifacts.is_relative_to(self.workspace):
            raise ValueError('Artifact directory must be inside the workspace')
        artifacts.mkdir(exist_ok=True)
        for name, data in {'runtime-evals-summary.md': summary.encode(),
                           'runtime-evals-report.json.gz': gzip.compress(
                               (directory / 'report.json').read_bytes(), mtime=0)}.items():
            target = artifacts / name
            if target.is_symlink():
                raise ValueError('Artifact symlinks are not supported')
            target.write_bytes(data)
        if publish and not receipt['published']:
            # The role's own MEMBER key writes annotations. No admin fallback.
            try:
                receipt['annotations'] = annotate(api, spans)
                receipt['published'] = True
                receipt.pop('status', None)
                receipt.pop('error_type', None)
            except (RuntimeError, ValueError, OSError) as exc:
                receipt.update(status='failed_or_unknown', annotations=None,
                               error_type=type(exc).__name__)
            save(directory / 'publication.json', receipt)
        return {**receipt, 'reused': reused, 'evaluator_version': VERSION, 'provenance': provenance,
                'summary': report['summary'], 'audit': report['audit'],
                'artifacts': ['runtime-evals-summary.md', 'runtime-evals-report.json.gz']}

    def read(self, run_id, section='summary'):
        allowed = {'summary', 'audit', 'by_kind', 'by_role', 'by_model', 'by_tool',
                   'sessions', 'limitations', 'provenance', 'publication'}
        if section not in allowed:
            raise ValueError('Unknown report section')
        directory = self.directory(run_id)
        path = directory / ('publication.json' if section == 'publication' else 'report.json')
        if path.is_symlink():
            raise ValueError('Report symlinks are not supported')
        report = json.loads(path.read_text())
        return report if section == 'publication' else report[section]


def create_server():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer('Popot Runtime Evals')
    runner = RuntimeEvals()

    @server.tool()
    def run_runtime_evals(hours: float = 12, end: str | None = None, publish: bool = True) -> str:
        """Run the versioned CODE evaluator on existing Phoenix spans using the role key.

        Captures a fixed window up to 7 days, at most 20000 spans, within 90 seconds.
        Saves a run_id, evidence snapshot and native report artifacts. publish=true
        upserts harness.* annotations using PHOENIX_API_KEY (MEMBER required).
        publish=false supports VIEWER analysis. This runs no agents/model judge.
        Reuse the returned run_id with read_eval_report for detailed analysis.
        """
        return json.dumps(runner.run(hours, end, publish), ensure_ascii=False)

    @server.tool()
    def read_eval_report(run_id: str, section: Literal[
            'summary', 'audit', 'by_kind', 'by_role', 'by_model', 'by_tool',
            'sessions', 'limitations', 'provenance', 'publication'] = 'summary') -> str:
        """Read one section of this session's saved eval report without rerunning it.

        Start with audit and limitations; use by_role/by_model/by_tool/sessions
        for breakdowns and publication to inspect the confirmed write receipt.
        """
        return json.dumps(runner.read(run_id, section), ensure_ascii=False)

    return server


if __name__ == '__main__':
    create_server().run(transport='stdio')
