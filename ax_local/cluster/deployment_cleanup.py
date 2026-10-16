"""Remove disposable local deployment resources, retaining data and rollback."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


IMAGE_LABEL = 'com.popot.project=popot-agents'


def cleanup_cluster(kubeconfig: Path, context: str, namespaces: list[str], *,
                    run=subprocess.run) -> int:
    if not re.fullmatch(r'kind-[a-z0-9-]+', context):
        raise ValueError('Deployment cleanup requires a local kind context')
    if any(not re.fullmatch(r'[a-z0-9][a-z0-9-]*', n) for n in namespaces):
        raise ValueError('Invalid cleanup namespace')
    removed = 0
    for namespace in dict.fromkeys(namespaces):
        kube = ['kubectl', f'--kubeconfig={kubeconfig}', f'--context={context}',
                '--request-timeout=15s', '-n', namespace]
        def execute(args, **kw):
            return run(args, text=True, capture_output=True, check=True, timeout=30, **kw)

        items = json.loads(execute([*kube, 'get', 'deployments,replicasets,jobs,pods',
                                   '-o', 'json']).stdout)['items']
        deployments = {o['metadata']['uid']: o for o in items if o['kind'] == 'Deployment'}
        jobs = {o['metadata']['uid']: o for o in items if o['kind'] == 'Job'}
        retain = set()
        for uid in deployments:
            replicas = [o for o in items if o['kind'] == 'ReplicaSet' and any(
                r.get('controller') is True and r.get('kind') == 'Deployment' and r.get('uid') == uid
                for r in o['metadata'].get('ownerReferences', []))]
            # The latest revision plus one previous revision remain available for rollback.
            revisions = sorted({int(o['metadata'].get('annotations', {}).get(
                'deployment.kubernetes.io/revision', '0')) for o in replicas}, reverse=True)
            retain.update(o['metadata']['uid'] for o in replicas if int(o['metadata'].get(
                'annotations', {}).get('deployment.kubernetes.io/revision', '0')) in revisions[:2])

        for obj in items:
            kind, meta = obj['kind'], obj['metadata']
            if meta.get('deletionTimestamp'):
                continue
            status = obj.get('status', {})
            owners = meta.get('ownerReferences', [])
            disposable = False
            if kind == 'ReplicaSet':
                disposable = (meta['uid'] not in retain and obj.get('spec', {}).get('replicas') == 0
                              and status.get('replicas', 0) == 0 and any(
                                  r.get('kind') == 'Deployment' and r.get('controller') is True
                                  and r.get('uid') in deployments for r in owners))
                endpoint = 'apps/v1'
                plural = 'replicasets'
            elif kind == 'Job':
                disposable = not status.get('active') and any(
                    c.get('type') in ('Complete', 'Failed') and c.get('status') == 'True'
                    for c in status.get('conditions', []))
                endpoint, plural = 'batch/v1', 'jobs'
            elif kind == 'Pod':
                # A completed Job removes its own pods through Kubernetes GC; avoid a
                # second delete racing that cascade. Live Job pods belong to its lifecycle.
                owned_by_job = any(r.get('kind') == 'Job' and r.get('uid') in jobs for r in owners)
                disposable = status.get('phase') in ('Succeeded', 'Failed') and not owned_by_job
                endpoint, plural = None, 'pods'
            if not disposable:
                continue
            prefix = f'/apis/{endpoint}' if endpoint else '/api/v1'
            options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'propagationPolicy': 'Background',
                       'preconditions': {'uid': meta['uid'], 'resourceVersion': meta['resourceVersion']}}
            execute([*kube, 'delete', '--raw', f'{prefix}/namespaces/{namespace}/{plural}/{meta["name"]}',
                     '-f', '-'], input=json.dumps(options))
            removed += 1
    print(f'Local Kubernetes deployment leftovers removed: {removed}', flush=True)
    return removed


def cleanup_docker(root: Path, *, run=subprocess.run) -> None:
    def execute(args, *, check=True):
        return run(args, capture_output=True, text=True, check=check, timeout=30)

    directories = {str(root.resolve()), str((root / 'ax_local').resolve()), str((root / 'memory').resolve())}
    projects = {'popot-agents', 'popot-ax-local', 'popot-memory'}
    removed = 0
    for cid in execute(['docker', 'ps', '-aq']).stdout.split():
        inspected = execute(['docker', 'inspect', cid], check=False)
        if inspected.returncode:
            continue  # A concurrent compose may already have removed it.
        container = json.loads(inspected.stdout)[0]
        labels = container.get('Config', {}).get('Labels') or {}
        state = container.get('State', {})
        if (labels.get('com.docker.compose.project') in projects
                and labels.get('com.docker.compose.project.working_dir') in directories
                and state.get('Status') in ('exited', 'dead', 'created') and not state.get('Running')):
            # Docker refuses to remove a container started since the inspection; never force.
            execute(['docker', 'rm', cid])
            removed += 1
    # Let the engine select dangling images atomically, so a concurrent build
    # tagging an image cannot make us remove its new tag. No --all and no volumes.
    execute(['docker', 'image', 'prune', '--force', '--filter', f'label={IMAGE_LABEL}'])
    print(f'Local Docker leftovers removed: containers={removed}; project dangling images pruned', flush=True)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / 'ax_local/config.json').read_text())['ax']
    cleanup_cluster(root / 'ax_local/.local/kubeconfig', config['context'],
                    ['ate-system', 'ax-system', config['atespace']])
    cleanup_docker(root)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        raise SystemExit('Local deployment cleanup failed: ' + detail)
