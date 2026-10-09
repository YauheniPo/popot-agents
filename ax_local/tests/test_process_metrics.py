import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from ax_local.worker.process_metrics import ProcessMetrics, sample_processes, make_payload


class ProcessMetricsTests(unittest.TestCase):
    def test_proc_stat_handles_spaces_parentheses_and_disappearing_pids(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / '41').mkdir()
            # Fields 3..24: state, ppid, ..., utime, stime, ..., starttime, vsize, rss.
            fields = ['S'] + ['0'] * 21
            fields[11:13] = ['150', '50']
            fields[17] = '3'
            fields[19:22] = ['900', '8192', '2']
            (proc / '41/stat').write_text('41 (python (worker)) ' + ' '.join(fields))
            (proc / '42').mkdir()  # Exited between listing and read.
            (proc / '41/cmdline').write_text('SECRET_TOKEN=do-not-export')
            samples = sample_processes(proc, clock_ticks=100, page_size=4096)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]['name'], 'python (worker)')
        self.assertEqual(samples[0]['cpu_seconds'], 2)
        self.assertEqual(samples[0]['rss_bytes'], 8192)
        self.assertEqual(samples[0]['start_ticks'], 900)
        self.assertEqual(samples[0]['threads'], 3)
        self.assertNotIn('SECRET_TOKEN', json.dumps(samples))

    def test_otlp_payload_has_cumulative_cpu_and_pid_reuse_identity(self):
        samples = [dict(pid=41, name='python', start_ticks=900, cpu_seconds=2.5,
                        rss_bytes=8192, virtual_bytes=16384, threads=3)]
        payload = make_payload(samples, task='popot-chat-abc', timestamp_ns=123456789)
        metrics = payload['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        cpu = next(m for m in metrics if m['name'] == 'popot.sandbox.process.cpu')
        self.assertEqual(cpu['sum']['aggregationTemporality'], 2)
        self.assertTrue(cpu['sum']['isMonotonic'])
        point = cpu['sum']['dataPoints'][0]
        self.assertEqual(point['timeUnixNano'], '123456789')
        labels = {a['key']: a['value']['stringValue'] for a in point['attributes']}
        self.assertEqual(labels['task'], 'popot-chat-abc')
        self.assertEqual(labels['process_start'], '900')
        self.assertEqual(labels['pid'], '41')
        self.assertTrue(any(m['name'] == 'popot.sandbox.process.sampled_at' for m in metrics))

    def test_disabled_exporter_never_starts_thread(self):
        with patch.dict('os.environ', {}, clear=True), patch('threading.Thread') as thread:
            with ProcessMetrics.from_env():
                pass
        thread.assert_not_called()

    def test_thread_stops_even_when_harness_fails(self):
        with patch('ax_local.worker.process_metrics.threading.Thread') as thread:
            exporter = ProcessMetrics('http://collector/v1/metrics', 'task')
            with self.assertRaises(ValueError):
                with exporter:
                    raise ValueError('harness failed')
        self.assertTrue(exporter.stop.is_set())
        thread.return_value.join.assert_called_once()

    def test_collector_failure_does_not_escape_and_does_not_queue(self):
        exporter = ProcessMetrics('http://collector/v1/metrics', 'task')
        with patch('ax_local.worker.process_metrics.build_opener') as client, \
             patch('ax_local.worker.process_metrics.sample_processes', return_value=[]), \
             patch.object(exporter.stop, 'wait', side_effect=lambda _: exporter.stop.set()):
            client.return_value.open.side_effect = URLError('offline')
            exporter._run()
        client.return_value.open.assert_called_once()
        self.assertTrue(exporter.stop.is_set())


if __name__ == '__main__':
    unittest.main()
