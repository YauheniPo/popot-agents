"""Run an on-demand HolmesGPT investigation of local AX pods.

Usage: python3 -m ax_local.cluster.holmes "Why are AX workers restarting?"
"""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time

from popot_agents.orchestrator.main import load_dotenv
from ax_local.cluster.build_versions import BUILD_VERSIONS

ROOT = Path(__file__).resolve().parents[2]
NAMESPACE = 'ax-diagnostics'
IMAGE = 'robustadev/holmes:' + BUILD_VERSIONS['HOLMES_VERSION']


def scope(config):
    if not re.fullmatch(r'kind-[a-z0-9-]+', config['context']):
        raise ValueError('Holmes requires a local kind context')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', config['atespace']):
        raise ValueError('Invalid AX worker namespace')
    return ('ax-system', 'ate-system', config['atespace'],
            'otel-system', 'ax-diagnostics', 'kube-system')


def manifests(config):
    namespaces = scope(config)
    objects = [
        {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': NAMESPACE}},
        {'apiVersion': 'v1', 'kind': 'ServiceAccount',
         'metadata': {'name': 'holmes', 'namespace': NAMESPACE}},
    ]
    for namespace in namespaces:
        objects.extend([
            {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role',
             'metadata': {'name': 'holmes-ax-reader', 'namespace': namespace},
             'rules': [{'apiGroups': [''], 'resources': ['pods', 'pods/log', 'events'],
                        'verbs': ['get', 'list', 'watch']}]},
            {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding',
             'metadata': {'name': 'holmes-ax-reader', 'namespace': namespace},
             'roleRef': {'apiGroup': 'rbac.authorization.k8s.io',
                         'kind': 'Role', 'name': 'holmes-ax-reader'},
             'subjects': [{'kind': 'ServiceAccount', 'name': 'holmes', 'namespace': NAMESPACE}]},
        ])
    tools = []
    columns = ','.join([
        'NAME:.metadata.name', 'PHASE:.status.phase', 'CONTAINERS:.spec.containers[*].name',
        'READY:.status.containerStatuses[*].ready', 'RESTARTS:.status.containerStatuses[*].restartCount',
        'WAITING:.status.containerStatuses[*].state.waiting.reason',
        'TERMINATED:.status.containerStatuses[*].state.terminated.reason',
        'LAST_REASON:.status.containerStatuses[*].lastState.terminated.reason',
        'LAST_EXIT:.status.containerStatuses[*].lastState.terminated.exitCode',
        'LAST_FINISHED:.status.containerStatuses[*].lastState.terminated.finishedAt',
        'CPU_REQUEST:.spec.containers[*].resources.requests.cpu',
        'CPU_LIMIT:.spec.containers[*].resources.limits.cpu',
        'MEMORY_REQUEST:.spec.containers[*].resources.requests.memory',
        'MEMORY_LIMIT:.spec.containers[*].resources.limits.memory',
    ])
    for name, description, command in [
        ('ax_pods', 'Read AX pod readiness, restarts, termination reasons and resources.',
         'get pods -o ' + shlex.quote('custom-columns=' + columns)),
        ('ax_events', 'Read recent Kubernetes events for AX pods.',
         'get events --sort-by=.lastTimestamp -o json'),
        ('ax_pod_logs', 'Read the last 200 log lines of an AX pod container.',
         'logs {{ pod }} -c {{ container }} --tail=200 --timestamps=true'),
        ('ax_previous_pod_logs', 'Read the last 200 log lines of a previous, terminated AX container.',
         'logs {{ pod }} -c {{ container }} --previous --tail=200 --timestamps=true'),
    ]:
        parameters = {'namespace': {'type': 'string', 'enum': list(namespaces)}}
        if '{{ pod }}' in command:
            parameters.update({'pod': {'type': 'string'}, 'container': {'type': 'string'}})
        tools.append({'name': name, 'description': description,
                      'command': 'kubectl --request-timeout=20s -n {{ namespace }} ' + command,
                      'parameters': parameters})
    toolsets = {'ax/pods': {'type': 'custom', 'enabled': True,
                           'description': 'Read-only diagnosis of local AX pods.', 'tools': tools,
                           'prerequisites': [{'command': f'kubectl --request-timeout=15s -n {ns} get pods -o name'}
                                             for ns in namespaces]}}
    account = '/var/run/secrets/kubernetes.io/serviceaccount/'
    kubeconfig = {
        'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'holmes',
        'clusters': [{'name': 'kind-ax', 'cluster': {
            'server': 'https://kubernetes.default.svc', 'certificate-authority': account + 'ca.crt'}}],
        'users': [{'name': 'holmes', 'user': {'tokenFile': account + 'token'}}],
        'contexts': [{'name': 'holmes', 'context': {'cluster': 'kind-ax', 'user': 'holmes'}}],
    }
    objects.append({'apiVersion': 'v1', 'kind': 'ConfigMap',
                    'metadata': {'name': 'holmes-ax', 'namespace': NAMESPACE},
                    'data': {'tools.json': json.dumps(toolsets), 'kubeconfig.json': json.dumps(kubeconfig),
                             'runner.py': (ROOT / 'ax_local/holmes/runner.py').read_text(),
                             'settings.json': (ROOT / 'config/holmes.json').read_text()}})
    return objects


def investigation_job(question, model):
    settings = json.loads((ROOT / 'config/holmes.json').read_text())
    return {'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': {'generateName': 'holmes-ax-', 'namespace': NAMESPACE},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': settings['active_deadline_seconds'],
                     'ttlSecondsAfterFinished': settings['ttl_seconds_after_finished'],
                     'template': {'spec': {
                         'serviceAccountName': 'holmes', 'restartPolicy': 'Never',
                         'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001,
                                             'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
                         'containers': [{
                             'name': 'holmes', 'image': IMAGE,
                             'command': ['python', '/etc/popot-holmes/runner.py', question],
                             'env': [
                                 {'name': 'HOLMES_MODEL', 'value': model},
                                 {'name': 'OPENAI_API_BASE', 'value': settings['api_base']},
                                 {'name': 'OPENAI_API_KEY', 'valueFrom': {'secretKeyRef': {
                                     'name': 'holmes-omniroute', 'key': 'api-key'}}},
                                 {'name': 'HOME', 'value': '/tmp'},
                                 {'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'},
                                 {'name': 'PYTHONUNBUFFERED', 'value': '1'},
                                 {'name': 'PYTHONPATH', 'value': '/app'},
                                 {'name': 'KUBECONFIG', 'value': '/etc/popot-holmes/kubeconfig.json'},
                                 {'name': 'OVERRIDE_MAX_OUTPUT_TOKEN', 'value': str(settings['max_output_tokens'])},
                             ],
                             'resources': settings['resources'],
                             'securityContext': {'allowPrivilegeEscalation': False,
                                                 'readOnlyRootFilesystem': True,
                                                 'capabilities': {'drop': ['ALL']}},
                             'volumeMounts': [{'name': 'config', 'mountPath': '/etc/popot-holmes', 'readOnly': True},
                                              {'name': 'tmp', 'mountPath': '/tmp'}],
                         }],
                         'volumes': [{'name': 'config', 'configMap': {'name': 'holmes-ax'}},
                                     {'name': 'tmp', 'emptyDir': {'sizeLimit': '512Mi'}}],
                     }}}}


def wait_for_pod(kube, job):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        pods = json.loads(kube('-n', NAMESPACE, 'get', 'pods',
                               '-l', f'job-name={job}', '-o', 'json'))['items']
        for pod in pods:
            if pod['status']['phase'] in ('Running', 'Succeeded'):
                return pod['metadata']['name']
            if pod['status']['phase'] == 'Failed':
                raise RuntimeError(f'Holmes pod {pod["metadata"]["name"]} failed; inspect its logs')
        time.sleep(2)
    raise TimeoutError('Holmes pod did not start within 300 seconds; inspect its events')


def ensure_image(node, architecture):
    cached = subprocess.run(['docker', 'exec', node, 'ctr', '-n', 'k8s.io', 'images', 'ls', '-q'],
                            check=True, text=True, capture_output=True, timeout=30).stdout.splitlines()
    if 'docker.io/' + IMAGE in cached:
        return
    platform = 'linux/' + architecture
    # Docker Desktop's daemon can pull images even when kind's containerd cannot.
    subprocess.run(['docker', 'pull', '--platform', platform, IMAGE], check=True, timeout=600)
    with tempfile.TemporaryFile() as archive:
        subprocess.run(['docker', 'save', '--platform', platform, IMAGE],
                       stdout=archive, check=True, timeout=300)
        archive.seek(0)
        subprocess.run(['docker', 'exec', '-i', node, 'ctr', '-n', 'k8s.io',
                        'images', 'import', '--platform', platform, '-'],
                       stdin=archive, check=True, timeout=300)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('question', nargs='?')
    parser.add_argument('--prepare', action='store_true', help='Cache image and prepare read-only tools without a model call')
    args = parser.parse_args()
    if not args.question and not args.prepare:
        parser.error('question is required unless --prepare is used')
    config = json.loads((ROOT / 'ax_local/config.json').read_text())
    scope(config)
    load_dotenv(ROOT / '.env')
    model = os.environ.get('HOLMES_MODEL', '').strip()
    if not model:
        raise ValueError('Set HOLMES_MODEL in .env before running Holmes')
    key = os.environ.get('OMNIROUTE_API_KEY', '').strip()
    if not key:
        raise ValueError('Set OMNIROUTE_API_KEY in .env before running Holmes')
    command = ['kubectl', f'--kubeconfig={ROOT / "ax_local/.local/kubeconfig"}',
               f'--context={config["context"]}']

    def kube(*arguments, payload=None):
        return subprocess.run(command + ['--request-timeout=30s'] + list(arguments), check=True, text=True,
                              input=json.dumps(payload) if payload is not None else None,
                              capture_output=True, timeout=60).stdout

    nodes = json.loads(kube('get', 'nodes', '-o', 'json'))['items']
    expected = config['context'].removeprefix('kind-') + '-control-plane'
    if len(nodes) != 1 or nodes[0]['metadata']['name'] != expected:
        raise ValueError('Expected the single-node local AX kind cluster')
    ensure_image(expected, nodes[0]['status']['nodeInfo']['architecture'])
    print(kube('apply', '--server-side', '-f', '-', payload={
        'apiVersion': 'v1', 'kind': 'List', 'items': manifests(config)}).strip(), flush=True)
    kube('apply', '--server-side', '-f', '-', payload={
        'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
        'metadata': {'name': 'holmes-omniroute', 'namespace': NAMESPACE},
        'stringData': {'api-key': key}})
    if args.prepare:
        print('Holmes image and project read-only tools prepared', flush=True)
        return
    job = json.loads(kube('create', '-f', '-', '-o', 'json',
                         payload=investigation_job(args.question, model)))['metadata']['name']
    print(f'Holmes job: {NAMESPACE}/{job}; model: {model}', flush=True)
    try:
        pod = wait_for_pod(kube, job)
        subprocess.run(command + ['-n', NAMESPACE, 'logs', '-f', pod], check=True, timeout=660)
        kube('-n', NAMESPACE, 'wait', '--for=condition=complete', f'job/{job}', '--timeout=15s')
    except (KeyboardInterrupt, TimeoutError, subprocess.TimeoutExpired):
        kube('-n', NAMESPACE, 'delete', 'job', job, '--wait=false')
        raise


if __name__ == '__main__':
    main()
