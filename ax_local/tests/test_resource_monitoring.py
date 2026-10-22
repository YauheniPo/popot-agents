import unittest
import subprocess
import json
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch
from ax_local.cluster import resource_monitoring as monitoring

from ax_local.cluster.resource_monitoring import extend_prometheus_config, local_context, migrate_prometheus_storage
from ax_local.cluster.resource_dashboard import dashboard


class ResourceMonitoringTests(unittest.TestCase):
    def test_forward_check_preserves_a_healthy_tunnel(self):
        with patch.object(monitoring, 'grafana_healthy', return_value=True), \
             patch.object(monitoring.subprocess, 'Popen') as start, \
             patch.object(monitoring.subprocess, 'run') as processes:
            monitoring.ensure_forward()
        start.assert_not_called()
        processes.assert_not_called()

    def test_forward_check_restarts_only_this_projects_stuck_tunnel(self):
        cluster = MagicMock(command=['kubectl', '--kubeconfig=/project/kubeconfig',
                                      '--context=kind-test', '--request-timeout=30s'])
        command = ' '.join(cluster.command + ['-n', 'otel-system', 'port-forward',
                                              '--address=127.0.0.1', 'service/grafana', '3002:3000'])
        listing = f'123 {command}\n456 kubectl --context=other port-forward service/grafana 3002:3000\n'
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(monitoring, 'LOCAL', Path(directory)), \
             patch.object(monitoring, 'Cluster', return_value=cluster), \
             patch.object(monitoring, 'grafana_healthy', side_effect=[False, False, True]), \
             patch.object(monitoring.subprocess, 'run', return_value=MagicMock(stdout=listing)), \
             patch.object(monitoring.subprocess, 'Popen') as start, \
             patch('os.kill') as kill, patch.object(monitoring.time, 'sleep'):
            monitoring.ensure_forward()
        kill.assert_called_once_with(123, monitoring.signal.SIGTERM)
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0][-1], 'forward')
        self.assertTrue(start.call_args.kwargs['start_new_session'])

    def test_forward_check_reports_failed_recovery(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(monitoring, 'LOCAL', Path(directory)), \
             patch.object(monitoring, 'Cluster', return_value=MagicMock(command=['kubectl'])), \
             patch.object(monitoring, 'grafana_healthy', return_value=False), \
             patch.object(monitoring.subprocess, 'run', return_value=MagicMock(stdout='')), \
             patch.object(monitoring.subprocess, 'Popen'), \
             patch.object(monitoring.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(RuntimeError, 'Grafana.*3002'):
                monitoring.ensure_forward()

    def test_forward_check_skips_monitoring_that_is_not_installed(self):
        cluster = MagicMock()
        cluster.run.return_value = ''
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(monitoring, 'LOCAL', Path(directory)), \
             patch.object(monitoring, 'Cluster', return_value=cluster), \
             patch.object(monitoring, 'grafana_healthy', return_value=False), \
             patch.object(monitoring.subprocess, 'Popen') as start:
            monitoring.ensure_forward()
        start.assert_not_called()

    def test_health_probe_rejects_timeout_invalid_response_and_unhealthy_database(self):
        for value in (TimeoutError(), b'not json', b'[]', b'{"database":"failing"}'):
            with self.subTest(value=value):
                client = MagicMock()
                if isinstance(value, Exception):
                    client.open.side_effect = value
                else:
                    response = client.open.return_value.__enter__.return_value
                    response.status = 200
                    response.read.return_value = value
                with patch.object(monitoring.request, 'build_opener', return_value=client):
                    self.assertFalse(monitoring.grafana_healthy())
                self.assertEqual(client.open.call_args.kwargs['timeout'], 3)

    def test_resources_are_loaded_from_service_sections_in_monitoring_config(self):
        from ax_local.resource_config import load_monitoring
        resources={'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}}
        prometheus={'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'memory':'512Mi'}}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'config').mkdir()
            (root/'config/monitoring.json').write_text(json.dumps({
                'grafana':{'environment':{'GF_USERS_ALLOW_SIGN_UP':'false'},'resources':resources},
                'prometheus':{'retention_time':'7d','resources':prometheus},
                'opentelemetry-collector':{'resources':{}}}))
            self.assertEqual(load_monitoring(root),{
                'grafana':{'environment':{'GF_USERS_ALLOW_SIGN_UP':'false'},'resources':resources},
                'prometheus':{'retention_time':'7d','resources':prometheus},
                'opentelemetry-collector':{'resources':{}}})

    def test_partial_resource_settings_still_reject_invalid_values(self):
        from ax_local.resource_config import load_monitoring
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'config').mkdir()
            for resources in ({'limits':{'memory':'8Gi'}},{'requests':{'cpu':'0m'}},
                              {'requests':{'cpu':'2'},'limits':{'cpu':'1'}},
                              {'limits':{'gpu':'1'}}):
                with self.subTest(resources=resources):
                    (root/'config/monitoring.json').write_text(json.dumps({'prometheus':{'resources':resources}}))
                    with self.assertRaises(ValueError):load_monitoring(root)

    def test_custom_scrape_interval_is_applied_without_changing_other_jobs(self):
        original='scrape_configs:\n  - job_name: existing\n    scrape_interval: 5s\n'
        updated=extend_prometheus_config(original,scrape_interval='30s')
        self.assertTrue(updated.startswith(original))
        self.assertIn('job_name: popot-cadvisor\n    scrape_interval: 30s',updated)

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
