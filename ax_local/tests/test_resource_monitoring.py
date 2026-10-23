import unittest
import subprocess
from unittest.mock import MagicMock, patch

from ax_local.cluster.resource_monitoring import extend_prometheus_config, local_context, migrate_prometheus_storage
from ax_local.cluster.resource_dashboard import dashboard


class ResourceMonitoringTests(unittest.TestCase):
    def test_cluster_totals_include_all_namespaces_and_ignore_display_filters(self):
        result = dashboard()
        for title, metric, unit in [('Cluster CPU (cores)', 'container_cpu_usage_seconds_total', 'cores'),
                                    ('Cluster memory (working set)', 'container_memory_working_set_bytes', 'bytes')]:
            chart = next(p for p in result['panels'] if p['title'] == title)
            expression = chart['targets'][0]['expr']
            self.assertIn(metric, expression)
            self.assertIn('sum(', expression)
            self.assertIn('namespace!=""', expression)
            self.assertIn('container!="POD"', expression)
            for name in ('namespace', 'pod', 'role', 'task', 'process'):
                self.assertNotIn('$' + name, expression)
            self.assertEqual(chart['fieldConfig']['defaults']['unit'], unit)
            nodes = chart['targets'][1]['expr']
            self.assertIn(metric, nodes)
            self.assertIn('sum(', nodes)
            self.assertIn('id="/"', nodes)
            self.assertIn('job="popot-cadvisor"', nodes)
            for name in ('namespace', 'pod', 'role', 'task', 'process'):
                self.assertNotIn('$' + name, nodes)

    def test_role_filters_include_history_and_scope_tasks_and_processes(self):
        result = dashboard()
        variables = {v['name']: v for v in result['templating']['list']}
        self.assertIn('role', variables)
        self.assertIn('sampled_at_seconds', variables['role']['query'])
        self.assertEqual(variables['role']['refresh'], 2)  # Time-range changes.
        self.assertNotIn('< 20', variables['role']['query'])  # Include finished work.
        for name in ('task', 'process'):
            self.assertIn('role=~"$role"', variables[name]['query'])
        for chart in result['panels']:
            expression = chart['targets'][0]['expr']
            if 'popot_sandbox_process_' in expression:
                self.assertIn('role=~"$role"', expression)
        history = next(p for p in result['panels'] if p['title'] == 'Tasks by role in selected period')
        self.assertIn('max_over_time(', history['targets'][0]['expr'])
        self.assertIn('[$__range]', history['targets'][0]['expr'])
        self.assertNotIn('< 20', history['targets'][0]['expr'])

    def test_prometheus_extension_preserves_existing_jobs_and_is_idempotent(self):
        original = 'global:\n  scrape_interval: 15s\nscrape_configs:\n  - job_name: existing\n'
        updated = extend_prometheus_config(original)
        self.assertTrue(updated.startswith(original))
        self.assertIn('metrics/cadvisor', updated)
        self.assertIn('kube-state-metrics', updated)
        self.assertEqual(updated, extend_prometheus_config(updated))

    def test_context_guard_rejects_remote_clusters(self):
        self.assertEqual(local_context({'context': 'kind-popot-ax'}), 'kind-popot-ax')
        with self.assertRaises(ValueError):
            local_context({'context': 'production'})

    def test_failed_history_copy_resumes_writer_and_removes_helper(self):
        cluster = MagicMock(command=['kubectl'])
        prom = {'spec': {'template': {'spec': {'securityContext': {},
                'volumes': [{'name': 'storage', 'emptyDir': {}}],
                'containers': [{'image': 'prometheus'}]}}}}
        with patch('ax_local.cluster.resource_monitoring.subprocess.Popen') as source, \
             patch('ax_local.cluster.resource_monitoring.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('tar', 30)):
            with self.assertRaises(subprocess.TimeoutExpired):
                migrate_prometheus_storage(cluster, prom)
        calls = [c.args for c in cluster.run.call_args_list]
        self.assertIn(('-n', 'otel-system', 'exec', 'deployment/prometheus', '--', 'kill', '-CONT', '1'), calls)
        self.assertEqual(calls[-1], ('-n', 'otel-system', 'delete', 'pod', 'popot-prometheus-migrate', '--wait=false'))

    def test_process_charts_hide_cached_samples_after_exit(self):
        charts = [p for p in dashboard()['panels'] if p['title'].startswith('Sandbox')]
        self.assertEqual(len(charts), 3)
        self.assertTrue(all('sampled_at_seconds' in p['targets'][0]['expr'] for p in charts))
        self.assertTrue(all('< 20' in p['targets'][0]['expr'] for p in charts))


if __name__ == '__main__':
    unittest.main()
