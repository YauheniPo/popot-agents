"""Extend the existing local Prometheus with pod and sandbox resource metrics.

Usage: python3 ax_local/cluster/resource_monitoring.py install
       python3 ax_local/cluster/resource_monitoring.py forward
The forward command supervises a loopback-only Grafana port-forward; it exits
when another copy already holds the lock. No host PID namespace is required.
"""

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

if __package__:
    from .resource_dashboard import dashboard
else:
    from resource_dashboard import dashboard

ROOT = Path(__file__).resolve().parents[2]
if not __package__:
    sys.path.insert(0, str(ROOT))
from ax_local.resource_config import load_monitoring
from ax_local.cluster.build_versions import BUILD_VERSIONS

LOCAL = ROOT / 'ax_local/.local'
NS = 'otel-system'
BEGIN, END = '  # popot-resources begin', '  # popot-resources end'


def local_context(config: dict) -> str:
    context = config['context']
    if not context.startswith('kind-'):
        raise ValueError('resource monitoring only supports the local kind cluster')
    return context


def extend_prometheus_config(original: str, node: str = 'popot-ax-control-plane',
                             address: str = '172.24.0.2', *, scrape_interval: str = '15s') -> str:
    if BEGIN in original:
        start, end = original.index(BEGIN), original.index(END) + len(END)
        original = original[:start] + original[end:].lstrip('\n')
    # Keep the upstream metrics allow-list, adding only our sandbox metrics.
    original = original.replace('(substrate|otelcol)_.*', '(substrate|otelcol|popot)_.*')
    return original.rstrip('\n') + '\n' + f'''{BEGIN}
  - job_name: popot-cadvisor
    scrape_interval: {scrape_interval}
    metrics_path: /metrics/cadvisor
    scheme: https
    authorization:
      credentials_file: /var/run/secrets/kubernetes.io/serviceaccount/token
    tls_config:
      ca_file: /etc/kubelet-tls/ca.pem
      server_name: {node}
    static_configs:
      - targets: ['{address}:10250']
        labels:
          node: {node}
    metric_relabel_configs:
      - source_labels: [__name__]
        action: keep
        regex: 'container_(cpu_usage_seconds_total|cpu_cfs_.*|memory_working_set_bytes|memory_rss|memory_usage_bytes|spec_memory_limit_bytes)|machine_(cpu_cores|memory_bytes)'
  - job_name: popot-kube-state-metrics
    static_configs:
      - targets: ['kube-state-metrics.otel-system.svc:8080']
{END}
'''


class Cluster:
    def __init__(self):
        config = json.loads((ROOT / 'ax_local/config.json').read_text())
        self.context = local_context(config)
        self.command = ['kubectl', f'--kubeconfig={LOCAL / "kubeconfig"}',
                        f'--context={self.context}', '--request-timeout=30s']

    def run(self, *args, input=None):
        return subprocess.run(self.command + list(args), input=input, text=True,
                              check=True, capture_output=True, timeout=660).stdout

    def get(self, kind, name):
        return json.loads(self.run('-n', NS, 'get', kind, name, '-o', 'json'))

    def apply(self, objects):
        print(self.run('apply', '-f', '-', input=json.dumps(
            {'apiVersion': 'v1', 'kind': 'List', 'items': objects})).strip())

    def patch(self, kind, name, value):
        self.run('-n', NS, 'patch', kind, name, '--type=merge', '-p', json.dumps(value))


def obj(kind, name, **fields):
    return {'apiVersion': 'v1', 'kind': kind,
            'metadata': {'name': name, 'namespace': NS}, **fields}


def pvc(name, size):
    return obj('PersistentVolumeClaim', name, spec={
        'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': size}}})


def deployment(name, image, *, args=None, ports=None, env=None, volumes=None, mounts=None,
               service_account=None, uid=65534, resources):
    container = {'name': name, 'image': image, 'ports': ports or [],
                 'resources': resources,
                 'securityContext': {'allowPrivilegeEscalation': False,
                                     'capabilities': {'drop': ['ALL']}}}
    for key, value in [('args', args), ('env', env), ('volumeMounts', mounts)]:
        if value:
            container[key] = value
    spec = {'automountServiceAccountToken': bool(service_account),
            'securityContext': {'runAsNonRoot': True, 'runAsUser': uid, 'fsGroup': uid,
                                'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [container], 'volumes': volumes or []}
    if service_account:
        spec['serviceAccountName'] = service_account
    result = obj('Deployment', name, spec={'replicas': 1, 'strategy': {'type': 'Recreate'},
        'selector': {'matchLabels': {'app': name}},
        'template': {'metadata': {'labels': {'app': name}}, 'spec': spec}})
    result['apiVersion'] = 'apps/v1'
    return result


def migrate_prometheus_storage(cluster, prom):
    """Copy the existing TSDB while its writer is briefly stopped, then resume.

    The 30-second copy deadline is shorter than the existing liveness failure
    window. Both writer resumption and pipe cleanup happen even on failure.
    """
    spec = prom['spec']['template']['spec']
    if not any(v['name'] == 'storage' and 'emptyDir' in v for v in spec['volumes']):
        return
    name = 'popot-prometheus-migrate'
    helper = obj('Pod', name, spec={
        'restartPolicy': 'Never', 'automountServiceAccountToken': False,
        'securityContext': spec['securityContext'],
        'containers': [{'name': 'copy', 'image': spec['containers'][0]['image'],
                        'command': ['sh', '-c', 'sleep 180'],
                        'volumeMounts': [{'name': 'data', 'mountPath': '/data'}]}],
        'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'prometheus-resource-data'}}]})
    cluster.apply([helper])
    try:
        cluster.run('-n', NS, 'wait', '--for=condition=Ready', f'pod/{name}', '--timeout=60s')
        cluster.run('-n', NS, 'exec', 'deployment/prometheus', '--', 'kill', '-STOP', '1')
        try:
            source = subprocess.Popen(cluster.command + ['-n', NS, 'exec', 'deployment/prometheus',
                '--', 'tar', '-C', '/prometheus', '-cf', '-', '.'], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            try:
                subprocess.run(cluster.command + ['-n', NS, 'exec', '-i', name, '--',
                    'tar', '-C', '/data', '-xf', '-'], stdin=source.stdout,
                    capture_output=True, check=True, timeout=30)
                source.stdout.close()
                _, errors = source.communicate(timeout=3)
                if source.returncode:
                    raise RuntimeError('Prometheus history copy failed: ' + errors.decode())
            finally:
                if source.poll() is None:
                    source.kill()
                source.communicate()
        finally:
            cluster.run('-n', NS, 'exec', 'deployment/prometheus', '--', 'kill', '-CONT', '1')
    finally:
        cluster.run('-n', NS, 'delete', 'pod', name, '--wait=false')


def install():
    cluster = Cluster()
    settings = load_monitoring(ROOT)
    overrides = {name: service['resources'] for name, service in settings.items() if 'resources' in service}
    node = json.loads(cluster.run('get', 'nodes', '-o', 'json'))['items']
    expected = cluster.context.removeprefix('kind-') + '-control-plane'
    if len(node) != 1 or node[0]['metadata']['name'] != expected:
        raise ValueError('expected the single-node local AX kind cluster')
    address = next(a['address'] for a in node[0]['status']['addresses'] if a['type'] == 'InternalIP')
    # Pin the public serving certificate, keeping kubelet TLS verification on.
    cert = subprocess.run(['docker', 'exec', expected, 'openssl', 'x509', '-in',
                           '/var/lib/kubelet/pki/kubelet.crt', '-outform', 'PEM'],
                          text=True, capture_output=True, check=True, timeout=15).stdout
    role = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRole',
            'metadata': {'name': 'popot-resource-metrics'}, 'rules': [
                {'apiGroups': [''], 'resources': ['nodes/metrics'], 'resourceNames': [expected], 'verbs': ['get']}]}
    state_role = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRole',
                  'metadata': {'name': 'popot-resource-state'}, 'rules': [
                      {'apiGroups': [''], 'resources': ['nodes', 'pods'], 'verbs': ['get', 'list', 'watch']}]}
    def binding(name, account, role_name):
        return {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRoleBinding',
                'metadata': {'name': name},
                'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole',
                            'name': role_name},
                'subjects': [{'kind': 'ServiceAccount', 'name': account, 'namespace': NS}]}
    ksm = deployment('kube-state-metrics', 'registry.k8s.io/kube-state-metrics/kube-state-metrics:' + BUILD_VERSIONS['KUBE_STATE_METRICS_VERSION'],
                     resources=overrides['kube-state-metrics'],
                     args=['--resources=pods,nodes', '--port=8080'],
                     ports=[{'containerPort': 8080}], service_account='kube-state-metrics')
    ksm['spec']['template']['spec']['containers'][0]['readinessProbe'] = {
        'httpGet': {'path': '/healthz', 'port': 8080}, 'initialDelaySeconds': 5}
    cluster.apply([pvc('prometheus-resource-data', '2Gi'), pvc('grafana-resource-data', '1Gi'),
                   obj('ConfigMap', 'popot-kubelet-tls', data={'ca.pem': cert}), role, state_role,
                   binding('popot-resource-prometheus', 'prometheus', 'popot-resource-metrics'),
                   obj('ServiceAccount', 'kube-state-metrics'),
                   binding('popot-resource-state', 'kube-state-metrics', 'popot-resource-state'), ksm,
                   obj('Service', 'kube-state-metrics', spec={'selector': {'app': 'kube-state-metrics'},
                       'ports': [{'port': 8080, 'targetPort': 8080}]})])

    config = cluster.get('configmap', 'prometheus-config')['data']
    config['prometheus.yml'] = extend_prometheus_config(config['prometheus.yml'], expected, address,
        scrape_interval=settings['prometheus']['scrape_interval'])
    cluster.run('-n', NS, 'exec', '-i', 'deployment/prometheus', '--', 'promtool', 'check',
                'config', '--syntax-only', '/dev/stdin', input=config['prometheus.yml'])
    cluster.patch('configmap', 'prometheus-config', {'data': config})
    prom = cluster.get('deployment', 'prometheus')
    migrate_prometheus_storage(cluster, prom)
    spec = prom['spec']['template']['spec']
    containers = spec['containers']
    if 'prometheus' in overrides:
        containers[0]['resources'] = overrides['prometheus']
    args = [a for a in containers[0]['args'] if not a.startswith('--storage.tsdb.retention.')]
    containers[0]['args'] = args + ['--storage.tsdb.retention.time=' + settings['prometheus']['retention_time'],
                                     '--storage.tsdb.retention.size=' + settings['prometheus']['retention_size']]
    if not any(v['name'] == 'kubelet-tls' for v in spec['volumes']):
        spec['volumes'].append({'name': 'kubelet-tls', 'configMap': {'name': 'popot-kubelet-tls'}})
        containers[0]['volumeMounts'].append({'name': 'kubelet-tls', 'mountPath': '/etc/kubelet-tls', 'readOnly': True})
    for volume in spec['volumes']:
        if volume['name'] == 'storage':
            volume.clear()
            volume.update({'name': 'storage', 'persistentVolumeClaim': {'claimName': 'prometheus-resource-data'}})
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode() + cert.encode()).hexdigest()
    cluster.patch('deployment', 'prometheus', {'spec': {'strategy': {'type': 'Recreate', 'rollingUpdate': None},
        'template': {'metadata': {'annotations': {'popot.dev/resource-config': digest}}, 'spec': spec}}})

    provision = settings['grafana']['datasources']
    providers = settings['grafana']['dashboard_providers']
    data = {'datasources.yaml': json.dumps(provision), 'dashboards.yaml': json.dumps(providers),
            'resources.json': json.dumps(dashboard())}
    grafana = deployment('grafana', 'grafana/grafana:' + BUILD_VERSIONS['GRAFANA_VERSION'], uid=472,
        resources=overrides['grafana'],
        ports=[{'name': 'web', 'containerPort': 3000}],
        env=[{'name': k, 'value': v} for k, v in settings['grafana']['environment'].items()],
        volumes=[{'name': 'data', 'persistentVolumeClaim': {'claimName': 'grafana-resource-data'}},
                 {'name': 'config', 'configMap': {'name': 'popot-grafana-config'}}],
        mounts=[{'name': 'data', 'mountPath': '/var/lib/grafana'},
                *[{'name': 'config', 'mountPath': path, 'subPath': key, 'readOnly': True} for key, path in [
                  ('datasources.yaml', '/etc/grafana/provisioning/datasources/prometheus.yaml'),
                  ('dashboards.yaml', '/etc/grafana/provisioning/dashboards/popot.yaml'),
                  ('resources.json', '/var/lib/grafana/dashboards/resources.json')]]])
    grafana['spec']['template']['metadata']['annotations'] = {
        'popot.dev/resource-config': hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()}
    grafana['spec']['template']['spec']['containers'][0]['readinessProbe'] = {
        'httpGet': {'path': '/api/health', 'port': 3000}, 'initialDelaySeconds': 10}
    cluster.apply([obj('ConfigMap', 'popot-grafana-config', data=data), grafana,
                   obj('Service', 'grafana', spec={'selector': {'app': 'grafana'},
                       'ports': [{'port': 3000, 'targetPort': 3000}]})])
    if 'opentelemetry-collector' in overrides:
        collector = cluster.get('deployment', 'opentelemetry-collector')['spec']['template']['spec']
        collector['containers'][0]['resources'] = overrides['opentelemetry-collector']
        cluster.patch('deployment', 'opentelemetry-collector', {'spec': {'template': {'spec': collector}}})
    for name in ['prometheus', 'kube-state-metrics', 'grafana']:
        print(cluster.run('-n', NS, 'rollout', 'status', f'deployment/{name}', '--timeout=600s').strip())
    cluster.run('-n', NS, 'exec', 'deployment/prometheus', '--', 'promtool', 'check', 'config',
                '/etc/prometheus/prometheus.yml')
    LOCAL.mkdir(parents=True, exist_ok=True)
    with (LOCAL / 'resource-monitoring-forward.log').open('ab') as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'forward'],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    print('Resource dashboard: http://127.0.0.1:3002/d/popot-resources')


def forward():
    cluster = Cluster()
    LOCAL.mkdir(parents=True, exist_ok=True)
    with (LOCAL / 'resource-monitoring-forward.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        def stop_forward(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop_forward)
        while True:
            command = cluster.command + ['-n', NS, 'port-forward', '--address=127.0.0.1',
                                         'service/grafana', '3002:3000']
            child = subprocess.Popen(command)
            try:
                child.wait()
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
            time.sleep(5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'forward'])
    command = parser.parse_args().command
    try:
        install() if command == 'install' else forward()
    except subprocess.CalledProcessError as error:
        print(error.stderr or str(error), file=sys.stderr)
        raise SystemExit(1)
