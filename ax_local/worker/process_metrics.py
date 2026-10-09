"""Export sandbox /proc resource usage via OTLP HTTP without an SDK dependency.

Only PID, comm and resource counters are read; argv, environment and files are
never exported. Sampling happens inside gVisor, which hides guest PIDs from the
node. A stopped/suspended sandbox stops sampling without being woken up.
"""

import json
import os
from pathlib import Path
import threading
import time
from urllib.error import URLError
from urllib.request import Request, build_opener, ProxyHandler


def sample_processes(proc: Path = Path('/proc'), *, clock_ticks: int | None = None,
                     page_size: int | None = None) -> list[dict]:
    clock_ticks = clock_ticks or os.sysconf('SC_CLK_TCK')
    page_size = page_size or os.sysconf('SC_PAGE_SIZE')
    samples = []
    for path in proc.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            stat = (path / 'stat').read_text()
            begin, end = stat.index('('), stat.rindex(')')
            fields = stat[end + 2:].split()  # Index zero is proc field 3.
            samples.append({
                'pid': int(path.name), 'name': stat[begin + 1:end],
                'start_ticks': int(fields[19]),
                'cpu_seconds': (int(fields[11]) + int(fields[12])) / clock_ticks,
                'rss_bytes': max(0, int(fields[21])) * page_size,
                'virtual_bytes': int(fields[20]), 'threads': int(fields[17]),
            })
        except (OSError, ValueError, IndexError):
            # Exiting processes and permission-restricted entries are normal.
            continue
        if len(samples) >= 1024:
            break
    return samples


def make_payload(samples: list[dict], *, task: str, timestamp_ns: int,
                 role: str = 'unassigned') -> dict:
    metrics = []
    for suffix, unit, key in [('cpu', 's', 'cpu_seconds'),
                              ('memory', 'By', 'rss_bytes'),
                              ('virtual_memory', 'By', 'virtual_bytes'),
                              ('threads', '{thread}', 'threads'),
                              ('sampled_at', 's', None)]:
        points = []
        for process in samples:
            labels = dict(task=task, role=role, pid=str(process['pid']), process=process['name'],
                          process_start=str(process['start_ticks']))
            points.append({
                'attributes': [{'key': k, 'value': {'stringValue': v}}
                               for k, v in labels.items()],
                'timeUnixNano': str(timestamp_ns),
                'asDouble': process[key] if key else timestamp_ns / 1e9,
            })
        metric = {'name': f'popot.sandbox.process.{suffix}', 'unit': unit}
        if suffix == 'cpu':
            metric['sum'] = {'aggregationTemporality': 2, 'isMonotonic': True,
                             'dataPoints': points}
        else:
            metric['gauge'] = {'dataPoints': points}
        metrics.append(metric)
    return {'resourceMetrics': [{'resource': {'attributes': [
        {'key': 'service.name', 'value': {'stringValue': 'popot-sandbox'}}]},
        'scopeMetrics': [{'scope': {'name': 'popot.resource-monitoring'},
                          'metrics': metrics}]}]}


class ProcessMetrics:
    def __init__(self, endpoint: str = '', task: str = '', interval: float = 5,
                 *, role: str = 'unassigned'):
        self.endpoint, self.task, self.interval = endpoint, task, interval
        self.role = role
        self.stop = threading.Event()
        self.thread = None

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('POPOT_RESOURCE_METRICS_URL', ''),
                   os.environ.get('POPOT_RESOURCE_METRICS_TASK', ''),
                   role=os.environ.get('POPOT_RESOURCE_METRICS_ROLE') or 'unassigned')

    def __enter__(self):
        if self.endpoint and self.task:
            self.thread = threading.Thread(target=self._run, name='resource-metrics', daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)

    def _run(self):
        # A failed collector must never affect serving or accumulate a queue.
        client = build_opener(ProxyHandler({}))
        while not self.stop.is_set():
            try:
                body = make_payload(sample_processes(), task=self.task,
                                    role=self.role, timestamp_ns=time.time_ns())
                request = Request(self.endpoint, data=json.dumps(body).encode(),
                                  headers={'Content-Type': 'application/json'})
                with client.open(request, timeout=2) as response:
                    response.read(4096)
            except (OSError, URLError, ValueError):
                pass
            self.stop.wait(self.interval)
