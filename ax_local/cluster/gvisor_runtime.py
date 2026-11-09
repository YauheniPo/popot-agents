"""Install the pinned ARM64 gVisor archive before local AX worker checks."""

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import uuid


BUILDER = ('us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/'
           'default_aarch64@sha256:485fb1a8e7dd871774537a20fcae5f185708f07045ac3849805113029a6bdf19')
AWS_CLI = 'amazon/aws-cli:2.17.0@sha256:643507c10ada7964ca6157b3d799f030b90577643da9955d319a77399ed80d73'


def digest(stream):
    stream.seek(0)
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(chunk)
    return result.hexdigest()


def verify(archive, expected):
    with archive.open('rb') as stream:
        if digest(stream) != expected:
            raise ValueError(f'gVisor archive SHA256 mismatch: {archive}')


def build_archive(root, cache, commit, version, *, run=subprocess.run):
    """Build in the pinned toolchain, without host credentials or Docker socket."""
    partial = cache / 'gvisor.tar.zstd.partial'
    partial.unlink(missing_ok=True)
    volume = 'popot-gvisor-' + hashlib.sha256(str(cache.resolve()).encode()).hexdigest()[:16]
    name = 'popot-gvisor-build-' + uuid.uuid4().hex[:12]
    print(f'Building pinned ARM64 gVisor {commit}; first build can take several minutes.', flush=True)
    try:
        run(['docker', 'run', '--rm', '--init', '--name', name, '--memory=3g', '--cpus=4',
             '-v', str(root / 'ax_local/cluster/gvisor_build.sh') + ':/gvisor-build.sh:ro',
             '-v', str(cache) + ':/out', '-v', volume + ':/build-home/.cache/bazel',
             '--entrypoint', '/bin/bash', BUILDER, '/gvisor-build.sh', commit, version],
            check=True, timeout=3600)
    finally:
        run(['docker', 'rm', '--force', name], check=False, capture_output=True, timeout=30)
    return partial


def ensure_runtime(root, *, run=subprocess.run, machine=None):
    root = Path(root).resolve()
    ax = root / 'ax_local'
    context = json.loads((ax / 'config.json').read_text())['context']
    if not re.fullmatch(r'kind-[a-z0-9-]+', context):
        raise ValueError('gVisor installation requires a local kind context')
    if (machine or platform.machine()) not in ('arm64', 'aarch64'):
        print('Custom gVisor pin is ARM64-only; keeping the upstream runtime on this architecture.', flush=True)
        return {'installed': False, 'changed': False}
    manifest = json.loads((ax / 'gvisor-runtime.json').read_text())
    annotations = manifest['metadata']['annotations']
    commit = annotations['popot.dev/gvisor-commit']
    version = annotations['popot.dev/gvisor-version']
    asset = manifest['spec']['assets']['arm64']['gvisor']
    sha = asset['sha256']
    if (manifest['metadata']['name'] != 'gvisor-default'
            or manifest['spec']['sandboxClass'] != 'gvisor'
            or not re.fullmatch(r'[0-9a-f]{40}', commit)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+', version)
            or not version.endswith(commit[:12])
            or not re.fullmatch(r'[0-9a-f]{64}', sha)):
        raise ValueError('Invalid pinned gVisor build')
    key = f'popot-runtime/gvisor/{commit}/arm64/{sha}/gvisor.tar.zstd'
    if asset['url'] != 'gs://ate-snapshots/' + key:
        raise ValueError('Pinned gVisor archive must use its content-addressed local bucket URI')
    cache = ax / '.local/gvisor' / commit
    cache.mkdir(parents=True, exist_ok=True)
    with (cache.parent / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        archive = cache / 'gvisor.tar.zstd'
        if archive.exists():
            verify(archive, sha)
        kube = ['kubectl', '--kubeconfig=' + str(ax / '.local/kubeconfig'),
                '--context=' + context, '--request-timeout=30s']

        def get(*args):
            return json.loads(run([*kube, *args, '-o', 'json'], check=True,
                                  text=True, capture_output=True, timeout=60).stdout)

        run([*kube, '-n', 'ate-system', 'rollout', 'status', 'deployment/rustfs',
             '--timeout=300s'], check=True, capture_output=True, timeout=330)
        deployment = get('-n', 'ate-system', 'get', 'deployment', 'rustfs')
        values = {}
        for entry in deployment['spec']['template']['spec']['containers'][0]['env']:
            name = entry['name']
            if name not in ('RUSTFS_ACCESS_KEY', 'RUSTFS_SECRET_KEY'):
                continue
            if 'value' in entry:
                values[name] = entry['value']
            else:
                ref = entry['valueFrom']['secretKeyRef']
                secret = get('-n', 'ate-system', 'get', 'secret', ref['name'])
                values[name] = base64.b64decode(secret['data'][ref['key']]).decode()
        if not all(values.get(k) for k in ('RUSTFS_ACCESS_KEY', 'RUSTFS_SECRET_KEY')):
            raise ValueError('Local RustFS credentials are missing')
        endpoint = 'http://' + get('-n', 'ate-system', 'get', 'service', 'rustfs')['spec']['clusterIP'] + ':9000'
        env = {**os.environ, 'AWS_ACCESS_KEY_ID': values['RUSTFS_ACCESS_KEY'],
               'AWS_SECRET_ACCESS_KEY': values['RUSTFS_SECRET_KEY'],
               'AWS_REGION': 'us-east-1', 'AWS_ENDPOINT_URL': endpoint}
        aws = ['docker', 'run', '--rm', '-i', '--network',
               'container:' + context.removeprefix('kind-') + '-control-plane']
        for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_REGION', 'AWS_ENDPOINT_URL'):
            aws.extend(['-e', name])
        aws.extend([AWS_CLI, '--cli-connect-timeout=10', '--cli-read-timeout=60'])
        uri = 's3://ate-snapshots/' + key
        # Completed init Jobs may be removed; the bucket is the durable prerequisite.
        run([*aws, 's3api', 'wait', 'bucket-exists', '--bucket', 'ate-snapshots'],
            env=env, check=True, stderr=subprocess.PIPE, timeout=300)

        def download():
            with tempfile.TemporaryFile() as stream:
                result = run([*aws, 's3', 'cp', uri, '-', '--only-show-errors'], env=env,
                             stdout=stream, stderr=subprocess.PIPE, check=False, timeout=300)
                if result.returncode:
                    if b'NoSuchKey' in result.stderr or b'(404)' in result.stderr:
                        return False
                    raise RuntimeError(f'gVisor download from local RustFS failed (exit {result.returncode})')
                if digest(stream) != sha:
                    raise ValueError('RustFS gVisor archive SHA256 mismatch')
                if not archive.exists():
                    stream.seek(0)
                    partial = cache / 'gvisor.tar.zstd.partial'
                    with partial.open('wb') as target:
                        shutil.copyfileobj(stream, target)
                    partial.replace(archive)
                return True

        remote_verified = download()
        if not archive.exists():
            partial = build_archive(root, cache, commit, version, run=run)
            verify(partial, sha)
            partial.replace(archive)
        if not remote_verified:
            with archive.open('rb') as stream:
                run([*aws, 's3', 'cp', '-', uri, '--only-show-errors'], env=env, stdin=stream,
                    check=True, stderr=subprocess.PIPE, timeout=300)
            if not download():
                raise RuntimeError('Uploaded gVisor archive is missing from RustFS')
        current = get('get', 'sandboxconfig', 'gvisor-default')
        if current['spec']['sandboxClass'] != 'gvisor':
            raise ValueError('Existing local runtime is not gVisor')
        desired_annotations = {**current['metadata'].get('annotations', {}), **annotations}
        changed = (current['spec']['assets'].get('arm64') != {'gvisor': asset}
                   or current['metadata'].get('annotations', {}) != desired_annotations)
        if changed:
            patch = [{'op': 'test', 'path': '/metadata/resourceVersion',
                      'value': current['metadata']['resourceVersion']},
                     {'op': 'add', 'path': '/spec/assets/arm64', 'value': {'gvisor': asset}},
                     {'op': 'add', 'path': '/metadata/annotations', 'value': desired_annotations}]
            run([*kube, 'patch', 'sandboxconfig', 'gvisor-default', '--type=json',
                 '-p', json.dumps(patch)], check=True, text=True, capture_output=True, timeout=60)
        actual = get('get', 'sandboxconfig', 'gvisor-default')
        if (actual['spec']['assets'].get('arm64') != {'gvisor': asset}
                or any(actual['metadata'].get('annotations', {}).get(k) != v for k, v in annotations.items())):
            raise RuntimeError('Local gVisor runtime read-back did not match the pinned build')
        print(f'Local ARM64 gVisor verified: {version}; SHA256 {sha}; changed={changed}', flush=True)
        return {'installed': True, 'changed': changed, 'sha256': sha, 'version': version}


if __name__ == '__main__':
    ensure_runtime(Path(__file__).resolve().parents[2])
