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
RUSTFS_SCHEMES = ('http', 'https')
RUSTFS_CREDENTIALS = ('RUSTFS_ACCESS_KEY', 'RUSTFS_SECRET_KEY')
AWS_ENV = ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_REGION', 'AWS_ENDPOINT_URL')


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


def rustfs_endpoint(cluster_ip):
    """Build the RustFS URL; local kind stays on plain HTTP unless TLS is configured."""
    scheme = os.environ.get('POPOT_RUSTFS_SCHEME', 'http').strip().lower()
    if scheme not in RUSTFS_SCHEMES:
        raise ValueError('POPOT_RUSTFS_SCHEME must be one of: ' + ', '.join(RUSTFS_SCHEMES))
    return f'{scheme}://{cluster_ip}:9000'


def load_pin(ax):
    """Read and validate the pinned runtime manifest."""
    manifest = json.loads((ax / 'gvisor-runtime.json').read_text())
    annotations = manifest['metadata']['annotations']
    commit = annotations['popot.dev/gvisor-commit']
    version = annotations['popot.dev/gvisor-version']
    asset = manifest['spec']['assets']['arm64']['gvisor']
    sha = asset['sha256']
    valid = (manifest['metadata']['name'] == 'gvisor-default'
             and manifest['spec']['sandboxClass'] == 'gvisor'
             and re.fullmatch(r'[0-9a-f]{40}', commit)
             and re.fullmatch(r'[A-Za-z0-9_.-]+', version)
             and version.endswith(commit[:12])
             and re.fullmatch(r'[0-9a-f]{64}', sha))
    if not valid:
        raise ValueError('Invalid pinned gVisor build')
    key = f'popot-runtime/gvisor/{commit}/arm64/{sha}/gvisor.tar.zstd'
    if asset['url'] != 'gs://ate-snapshots/' + key:
        raise ValueError('Pinned gVisor archive must use its content-addressed local bucket URI')
    return commit, version, annotations, asset, sha, key


def rustfs_credentials(get):
    """Resolve the RustFS credentials the deployment actually runs with."""
    deployment = get('-n', 'ate-system', 'get', 'deployment', 'rustfs')
    values = {}
    for entry in deployment['spec']['template']['spec']['containers'][0]['env']:
        name = entry['name']
        if name not in RUSTFS_CREDENTIALS:
            continue
        if 'value' in entry:
            values[name] = entry['value']
            continue
        ref = entry['valueFrom']['secretKeyRef']
        secret = get('-n', 'ate-system', 'get', 'secret', ref['name'])
        values[name] = base64.b64decode(secret['data'][ref['key']]).decode()
    if not all(values.get(name) for name in RUSTFS_CREDENTIALS):
        raise ValueError('Local RustFS credentials are missing')
    return values


def aws_command(context):
    """Run the pinned AWS CLI inside the kind control-plane network namespace."""
    command = ['docker', 'run', '--rm', '-i', '--network',
               'container:' + context.removeprefix('kind-') + '-control-plane']
    for name in AWS_ENV:
        command.extend(['-e', name])
    command.extend([AWS_CLI, '--cli-connect-timeout=10', '--cli-read-timeout=60'])
    return command


def download_archive(aws, uri, sha, archive, cache, run, env):
    """Fetch the pinned archive from RustFS; report whether the object exists."""
    partial = cache / 'gvisor.tar.zstd.partial'
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
            with partial.open('wb') as target:
                shutil.copyfileobj(stream, target)
            partial.replace(archive)
        return True


def upload_archive(aws, uri, archive, run, env):
    """Publish the verified archive without exposing credentials in the arguments."""
    with archive.open('rb') as stream:
        run([*aws, 's3', 'cp', '-', uri, '--only-show-errors'], env=env, stdin=stream,
            check=True, stderr=subprocess.PIPE, timeout=300)


def sync_archive(root, cache, commit, version, sha, aws, uri, env, run):
    """Ensure the pinned archive is verified locally and present in RustFS."""
    archive = cache / 'gvisor.tar.zstd'
    remote_verified = download_archive(aws, uri, sha, archive, cache, run, env)
    if not archive.exists():
        partial = build_archive(root, cache, commit, version, run=run)
        verify(partial, sha)
        partial.replace(archive)
    if remote_verified:
        return archive
    upload_archive(aws, uri, archive, run, env)
    if not download_archive(aws, uri, sha, archive, cache, run, env):
        raise RuntimeError('Uploaded gVisor archive is missing from RustFS')
    return archive


def apply_runtime(kube, runtime, asset, annotations, run):
    """Patch the sandbox config and confirm the pinned asset reads back."""
    current = runtime()
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
    actual = runtime()
    if (actual['spec']['assets'].get('arm64') != {'gvisor': asset}
            or any(actual['metadata'].get('annotations', {}).get(k) != v for k, v in annotations.items())):
        raise RuntimeError('Local gVisor runtime read-back did not match the pinned build')
    return changed


def ensure_runtime(root, *, run=subprocess.run, machine=None):
    root = Path(root).resolve()
    ax = root / 'ax_local'
    context = json.loads((ax / 'config.json').read_text())['context']
    if not re.fullmatch(r'kind-[a-z0-9-]+', context):
        raise ValueError('gVisor installation requires a local kind context')
    if (machine or platform.machine()) not in ('arm64', 'aarch64'):
        print('Custom gVisor pin is ARM64-only; keeping the upstream runtime on this architecture.', flush=True)
        return {'installed': False, 'changed': False}
    commit, version, annotations, asset, sha, key = load_pin(ax)
    cache = ax / '.local/gvisor' / commit
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / 'gvisor.tar.zstd'
    with (cache.parent / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if archive.exists():
            verify(archive, sha)
        kube = ['kubectl', '--kubeconfig=' + str(ax / '.local/kubeconfig'),
                '--context=' + context, '--request-timeout=30s']

        def get(*args):
            return json.loads(run([*kube, *args, '-o', 'json'], check=True,
                                  text=True, capture_output=True, timeout=60).stdout)

        def runtime():
            return get('get', 'sandboxconfig', 'gvisor-default')

        run([*kube, '-n', 'ate-system', 'rollout', 'status', 'deployment/rustfs',
             '--timeout=300s'], check=True, capture_output=True, timeout=330)
        values = rustfs_credentials(get)
        cluster_ip = get('-n', 'ate-system', 'get', 'service', 'rustfs')['spec']['clusterIP']
        env = {**os.environ, 'AWS_ACCESS_KEY_ID': values['RUSTFS_ACCESS_KEY'],
               'AWS_SECRET_ACCESS_KEY': values['RUSTFS_SECRET_KEY'],
               'AWS_REGION': 'us-east-1', 'AWS_ENDPOINT_URL': rustfs_endpoint(cluster_ip)}
        aws = aws_command(context)
        uri = 's3://ate-snapshots/' + key
        # Completed init Jobs may be removed; the bucket is the durable prerequisite.
        run([*aws, 's3api', 'wait', 'bucket-exists', '--bucket', 'ate-snapshots'],
            env=env, check=True, stderr=subprocess.PIPE, timeout=300)
        sync_archive(root, cache, commit, version, sha, aws, uri, env, run)
        changed = apply_runtime(kube, runtime, asset, annotations, run)
        print(f'Local ARM64 gVisor verified: {version}; SHA256 {sha}; changed={changed}', flush=True)
        return {'installed': True, 'changed': changed, 'sha256': sha, 'version': version}


def main():
    ensure_runtime(Path(__file__).resolve().parents[2])


if __name__ == '__main__':
    main()
