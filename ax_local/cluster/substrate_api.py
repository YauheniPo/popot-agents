"""Build the local Substrate API with serialized golden actor teardown."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

if __package__:
    from .build_versions import BUILD_VERSIONS
else:
    from build_versions import BUILD_VERSIONS

PIN = BUILD_VERSIONS['SUBSTRATE_COMMIT']
ANCHOR = '\tdeleted, err := s.impl.DeleteActorTemplate(ctx, templateRef)'
GUARD = '''\tctx, release, err := s.popotPrepareTemplateDeletion(ctx, templateRef)
\tif err != nil {
\t\treturn nil, err
\t}
\tdefer release()
'''


def patched_source(source):
    if source.count(ANCHOR) != 1:
        raise ValueError('Substrate template deletion source differs from pinned version')
    if 'popotPrepareTemplateDeletion' in source:
        if source.count(GUARD + ANCHOR) != 1:
            raise ValueError('Existing template cleanup patch differs')
        return source
    return source.replace(ANCHOR, GUARD + ANCHOR, 1)


def main():
    root = Path(__file__).resolve().parents[2]
    local = root / 'ax_local/.local'
    config = json.loads((root / 'ax_local/config.json').read_text())
    if not re.fullmatch(r'kind-[a-z0-9-]+', config['context']):
        raise ValueError('Template cleanup requires a local kind context')
    # A second up.sh must not race source writes, the image rollout or its stamp.
    with (local / 'substrate-api.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deploy(local, config)


def deploy(local, config):
    checkout = local / 'src/substrate'
    env = {**os.environ, 'KUBECONFIG': str(local / 'kubeconfig'), 'KO_DOCKER_REPO': 'localhost:5001'}

    def run(command, **kwargs):
        return subprocess.run(command, env=env, text=True, check=True, **kwargs)

    kube = ['kubectl', '--kubeconfig=' + str(local / 'kubeconfig'), '--context=' + config['context'],
            '-n', 'ate-system']
    if run(['git', 'rev-parse', 'HEAD'], cwd=checkout, capture_output=True).stdout.strip() != PIN:
        raise ValueError('Substrate checkout differs from pinned version')
    deployment = json.loads(run([*kube, 'get', 'deployment', 'ate-api-server', '-o', 'json'],
                                capture_output=True).stdout)
    containers = deployment['spec']['template']['spec']['containers']
    matches = [i for i, c in enumerate(containers) if c['name'] == 'ate-api-server']
    if len(matches) != 1:
        raise ValueError('Expected one ate-api-server container')
    index = matches[0]
    directory = checkout / 'cmd/ateapi/internal/controlapi'
    source_file = directory / 'actor_template.go'
    source = source_file.read_text()
    updated = patched_source(source)
    helper = Path(__file__).with_name('template_cleanup_go')
    names = ('popot_template_cleanup.go', 'popot_template_cleanup_test.go')
    # These generated helpers are owned by this build recipe. Refresh them
    # from the repository when rebuilding a newer revision.
    version = hashlib.sha256(PIN.encode() + Path(__file__).read_bytes() + b''.join(
        (helper / name).read_bytes() for name in names)).hexdigest()
    stamp = local / 'substrate-api.json'
    installed = json.loads(stamp.read_text()) if stamp.exists() else {}
    helpers_current = all((directory / name).is_file() and (directory / name).read_bytes() ==
                          (helper / name).read_bytes() for name in names)
    if (installed == {'version': version, 'image': containers[index]['image']}
            and source == updated and helpers_current):
        run([*kube, 'rollout', 'status', 'deployment/ate-api-server', '--timeout=300s'])
        print('Local template cleanup is current; build skipped.', flush=True)
        return
    source_file.write_text(updated)
    for name in names:
        (directory / name).write_bytes((helper / name).read_bytes())
    run(['gofmt', '-w', str(source_file), *[str(directory / name) for name in names]])
    run(['go', 'test', './cmd/ateapi/internal/controlapi', '-run', '^TestPopotTemplateDeletion$', '-count=1'],
        cwd=checkout)
    arch = run(['go', 'env', 'GOARCH'], capture_output=True).stdout.strip()
    env['KO_DEFAULTPLATFORMS'] = 'linux/' + arch
    image = run([str(local / 'bin/ko'), 'build', './cmd/ateapi'], cwd=checkout,
                stdout=subprocess.PIPE).stdout.strip()
    if not re.fullmatch(r'localhost:5001/[^\s]+@sha256:[0-9a-f]{64}', image):
        raise ValueError('Build did not return a pinned local image')
    patch = [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': deployment['metadata']['resourceVersion']},
        {'op': 'replace', 'path': f'/spec/template/spec/containers/{index}/image', 'value': image},
    ]
    run([*kube, 'patch', 'deployment', 'ate-api-server', '--type=json', '-p', json.dumps(patch)])
    run([*kube, 'rollout', 'status', 'deployment/ate-api-server', '--timeout=300s'])
    stamp.write_text(json.dumps({'version': version, 'image': image}) + '\n')
    print('Local template cleanup deployed.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc) if isinstance(exc, ValueError)
                         else 'Template cleanup deployment failed; inspect preceding output')
