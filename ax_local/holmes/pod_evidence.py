"""Bounded pod observations for Holmes, without environment or full pod specs.

This stdlib-only reader runs inside the pinned Holmes image via ConfigMap.
"""

from datetime import datetime, timezone
import json
import re
import subprocess
import sys

MAX_PODS = 100
MAX_CONTAINERS = 32
MAX_OUTPUT_CHARS = 24000


def termination(value):
    return {key: value[key] for key in ('reason', 'exitCode', 'startedAt', 'finishedAt')
            if key in value}


def container_state(value):
    if 'terminated' in value:
        return {'terminated': termination(value['terminated'])}
    if 'running' in value:
        return {'running': {key: value['running'][key] for key in ('startedAt',)
                            if key in value['running']}}
    if 'waiting' in value:
        return {'waiting': {key: value['waiting'][key] for key in ('reason',)
                            if key in value['waiting']}}
    return {}


def snapshot(payload, namespace):
    rows = payload['items']
    result = {'source_type': 'observation', 'observed_at': datetime.now(timezone.utc).isoformat(),
              'namespace': namespace, 'total_pods': len(rows), 'truncated': False, 'pods': []}
    output_chars = len(json.dumps(result))
    for row in rows[:MAX_PODS]:
        status = row.get('status', {})
        containers = []
        for kind, spec_key, status_key in (
                ('container', 'containers', 'containerStatuses'),
                ('init', 'initContainers', 'initContainerStatuses'),
                ('ephemeral', 'ephemeralContainers', 'ephemeralContainerStatuses')):
            by_name = {item['name']: item for item in status.get(status_key, [])}
            for container in row.get('spec', {}).get(spec_key, []):
                current = by_name.get(container['name'], {})
                previous = current.get('lastState', {}).get('terminated')
                resources = container.get('resources', {})
                containers.append({
                    'name': container['name'], 'type': kind,
                    'ready': current.get('ready'), 'restart_count': current.get('restartCount'),
                    'state': container_state(current.get('state', {})),
                    'previous_termination': termination(previous) if previous is not None else None,
                    'resources': {group: {key: values[key] for key in ('cpu', 'memory') if key in values}
                                  for group in ('requests', 'limits')
                                  if (values := resources.get(group)) is not None},
                })
        projected = {
            'pod': row['metadata']['name'],
            'created_at': row['metadata'].get('creationTimestamp'), 'phase': status.get('phase'),
            'conditions': [{key: condition[key] for key in
                            ('type', 'status', 'reason', 'lastTransitionTime') if key in condition}
                           for condition in status.get('conditions', [])
                           if condition.get('type') in ('Ready', 'ContainersReady', 'Initialized', 'PodScheduled')],
            'containers': containers[:MAX_CONTAINERS],
            'containers_truncated': len(containers) > MAX_CONTAINERS,
        }
        output_chars += len(json.dumps(projected)) + 2
        if output_chars > MAX_OUTPUT_CHARS:
            break
        result['pods'].append(projected)
    result['truncated'] = len(result['pods']) < len(rows)
    return result


def main():
    namespace = sys.argv[1]
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', namespace):
        raise ValueError('Invalid Kubernetes namespace')
    result = subprocess.run([
        'kubectl', '--request-timeout=20s', '-n', namespace, 'get', 'pods', '-o', 'json'],
        check=True, capture_output=True, text=True, timeout=25)
    print(json.dumps(snapshot(json.loads(result.stdout), namespace)))


if __name__ == '__main__':
    main()
