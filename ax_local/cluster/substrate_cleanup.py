"""Build and deploy local Substrate cleanup fixes; preview unless --apply is set.

Only the configured kind cluster and pinned Substrate source are supported.
The existing atelet DaemonSet gets a new image and DAC_OVERRIDE/FOWNER.
The configured gVisor worker pool rolls to an image with idempotent runsc
cleanup. Session database records are preserved.
"""

import argparse
import copy
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
START = '\tif _, err := client.TerminateWorkload(ctx, &ateompb.TerminateWorkloadRequest{'
END = '\n\t// Deregister after teardown succeeds'
GUARD = '''\talreadyReset := false
\tif atev1alpha1.SandboxClass(sandboxRec.SandboxClass) == atev1alpha1.SandboxClassGvisor {
\t\talreadyReset, err = popotWorkloadWasReset(ateompath.OCIBundleDir(actorUID), ateompath.RunSCStateDir(actorUID))
\t\tif err != nil {
\t\t\treturn nil, fmt.Errorf("checking previous workload cleanup: %w", err)
\t\t}
\t}
\tif !alreadyReset {
'''

RUNSC_LIST = '''
func (r *runsc) popotListContainers(ctx context.Context) ([]string, error) {
	var output strings.Builder
	cmd := exec.CommandContext(ctx, r.path,
		"-root", ateompath.RunSCStateDir(r.actorUID), "list", "-quiet")
	cmd.Stdout = &output
	cmd.Stderr = os.Stderr
	if err := reaper.RunCommand(cmd); err != nil {
		return nil, err
	}
	return strings.Fields(output.String()), nil
}
'''


def patch_runsc(source: str) -> str:
    for operation in ('state', 'delete'):
        original = f'return fmt.Errorf("while running `runsc {operation}`: %w", err)'
        updated = ('return popotIgnoreRemovedContainer(containerName, '
                   f'fmt.Errorf("while running `runsc {operation}`: %w", err), '
                   'func() ([]string, error) { return r.popotListContainers(ctx) })')
        if source.count(original) == 1:
            source = source.replace(original, updated, 1)
        elif source.count(updated) != 1:
            raise ValueError('Substrate runsc source differs from pinned version')
    if 'func (r *runsc) popotListContainers' not in source:
        if source.count('\t"slices"\n') != 1 or '\t"strings"\n' in source:
            raise ValueError('Unexpected runsc imports')
        source = source.replace('\t"slices"\n', '\t"slices"\n\t"strings"\n', 1) + RUNSC_LIST
    elif RUNSC_LIST not in source:
        raise ValueError('Existing runsc list patch differs')
    return source


def patch_terminate(source: str) -> str:
    if GUARD in source:
        original = source.replace(GUARD, '', 1)
        start, end = original.index('\t' + START), original.index(END)
        wrapped = original[start:end]
        if not wrapped.endswith('\t}\n'):
            raise ValueError("Unexpected local cleanup patch")
        original = (original[:start] + ''.join(line[1:] if line.startswith('\t') else line
                    for line in wrapped[:-3].splitlines(keepends=True)) + original[end:])
        if patch_terminate(original) != source:
            raise ValueError("Unexpected local cleanup patch")
        return source
    if source.count(START) != 1 or source.count(END) != 1 or 'popotWorkloadWasReset' in source:
        raise ValueError("Substrate termination source differs from pinned version")
    start, end = source.index(START), source.index(END)
    if end <= start:
        raise ValueError("Unexpected termination boundaries")
    block = ''.join('\t' + line if line.strip() else line
                    for line in source[start:end].splitlines(keepends=True))
    return source[:start] + GUARD + block + '\t}\n' + source[end:]


def daemonset_patch(ds: dict, image: str) -> list:
    containers = ds['spec']['template']['spec']['containers']
    matches = [(i, c) for i, c in enumerate(containers) if c['name'] == 'atelet']
    if len(matches) != 1:
        raise ValueError("Expected exactly one atelet container")
    index, container = matches[0]
    security = copy.deepcopy(container.get('securityContext', {}))
    if security.get('runAsUser') != 0 or security.get('runAsGroup') != 0:
        raise ValueError("Expected pinned atelet UID/GID 0")
    capabilities = security.setdefault('capabilities', {})
    added = capabilities.setdefault('add', [])
    for capability in ('DAC_OVERRIDE', 'FOWNER'):
        if capability not in added:
            added.append(capability)
    path = f'/spec/template/spec/containers/{index}'
    return [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': ds['metadata']['resourceVersion']},
        {'op': 'replace', 'path': path + '/image', 'value': image},
        {'op': 'add', 'path': path + '/securityContext', 'value': security},
    ]


def install_runsc_cleanup(root, checkout, config, kubectl, env, run):
    namespace, pool_name = config['atespace'], config['worker_pool_name']
    if not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name)
               for name in (namespace, pool_name)):
        raise ValueError('Invalid local worker pool identity')
    pool_base = [*kubectl, '-n', namespace]
    pool = json.loads(run([*pool_base, 'get', 'workerpool', pool_name, '-o', 'json'],
                          capture_output=True).stdout)
    if pool['spec']['sandboxClass'] != 'gvisor':
        raise ValueError('Runsc cleanup requires a gVisor worker pool')
    source_file = checkout / 'cmd/ateom-gvisor/runsc.go'
    source = source_file.read_text()
    updated = patch_runsc(source)
    helper = Path(__file__).with_name('gvisor_cleanup_go')
    names = ('popot_runsc_cleanup.go', 'popot_runsc_cleanup_test.go')
    for name in names:
        target = source_file.with_name(name)
        if target.exists() and target.read_bytes() != (helper / name).read_bytes():
            raise ValueError('Existing runsc cleanup helper differs; inspect before replacing')
    version = hashlib.sha256(Path(__file__).read_bytes() + (helper / names[0]).read_bytes()).hexdigest()
    stamp = root / 'ax_local/.local/gvisor-cleanup.json'
    try:
        installed = json.loads(stamp.read_text())
    except (FileNotFoundError, ValueError):
        installed = {}
    if (isinstance(installed, dict) and installed.get('version') == version and
            installed.get('image') == pool['spec']['workerImage'] and source == updated and
            all(source_file.with_name(name).exists() for name in names)):
        print('Local runsc cleanup fix is current; build skipped.', flush=True)
        image = pool['spec']['workerImage']
    else:
        source_file.write_text(updated)
        for name in names:
            source_file.with_name(name).write_bytes((helper / name).read_bytes())
        run(['gofmt', '-w', str(source_file)])
        run(['go', 'test', *names], cwd=source_file.parent)
        env['KO_DEFAULTPLATFORMS'] = 'linux/' + run(['go', 'env', 'GOARCH'], capture_output=True).stdout.strip()
        image = run([str(root / 'ax_local/.local/bin/ko'), 'build', './cmd/ateom-gvisor',
                     '--ldflags=-X=github.com/agent-substrate/substrate/internal/version.Version=popot-runsc-' + PIN[:12]],
                    cwd=checkout, stdout=subprocess.PIPE).stdout.strip()
        if not re.fullmatch(r'localhost:5001/[^\s]+@sha256:[0-9a-f]{64}', image):
            raise ValueError('Runsc build did not return a pinned local image')
        fresh = json.loads(run([*pool_base, 'get', 'workerpool', pool_name, '-o', 'json'],
                               capture_output=True).stdout)
        patch = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': fresh['metadata']['resourceVersion']},
                 {'op': 'replace', 'path': '/spec/workerImage', 'value': image}]
        run([*pool_base, 'patch', 'workerpool', pool_name, '--type=json', '-p', json.dumps(patch)])
    run([*pool_base, 'wait', 'deployment/' + pool_name,
         '--for=jsonpath={.spec.template.spec.containers[?(@.name=="ateom")].image}=' + image, '--timeout=180s'])
    run([*pool_base, 'rollout', 'status', 'deployment/' + pool_name, '--timeout=180s'])
    stamp.write_text(json.dumps({'version': version, 'image': image}) + '\n')
    print('Local runsc cleanup fix deployed.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    local = root / 'ax_local/.local'
    checkout = local / 'src/substrate'
    config = json.loads((root / 'ax_local/config.json').read_text())
    context = config['context']
    if not re.fullmatch(r'kind-[a-z0-9-]+', context):
        raise ValueError('Cleanup fix requires a local kind context')
    print('Local Substrate cleanup fixes: atelet and gVisor worker images; '
          'session database unchanged.', flush=True)
    if not args.apply:
        print('Preview only. Run with --apply to build and deploy in ' + context)
        return
    kubeconfig = local / 'kubeconfig'
    ko = local / 'bin/ko'
    if not kubeconfig.is_file() or not ko.is_file():
        raise ValueError('Local AX bootstrap is missing')
    env = {**os.environ, 'KUBECONFIG': str(kubeconfig), 'KO_DOCKER_REPO': 'localhost:5001'}

    def run(command, **kwargs):
        return subprocess.run(command, env=env, text=True, check=True, **kwargs)

    kubectl = ['kubectl', '--kubeconfig=' + str(kubeconfig), '--context=' + context]
    selected = run([*kubectl, 'config', 'current-context'], capture_output=True).stdout.strip()
    if selected != context:
        raise ValueError('Local kubeconfig current context differs from configured kind context')
    head = run(['git', 'rev-parse', 'HEAD'], cwd=checkout, capture_output=True).stdout.strip()
    if head != PIN:
        raise ValueError('Substrate checkout differs from pinned version')
    ds_base = [*kubectl, '-n', 'ate-system']
    items = json.loads(run([*ds_base, 'get', 'daemonsets', '-l', 'app=atelet', '-o', 'json'],
                          capture_output=True).stdout)['items']
    if len(items) != 1:
        raise ValueError('Expected one local atelet DaemonSet; inspect multiple versions manually')
    daemonset_patch(items[0], 'validation-only')
    main_file = checkout / 'cmd/atelet/main.go'
    source = main_file.read_text()
    updated = patch_terminate(source)
    helper = Path(__file__).with_name('cleanup_go')
    for name in ('popot_cleanup.go', 'popot_cleanup_test.go'):
        target = main_file.with_name(name)
        content = (helper / name).read_text()
        if target.exists() and target.read_text() != content:
            raise ValueError('Existing cleanup helper differs; inspect local source before replacing')
    version = hashlib.sha256(Path(__file__).read_bytes() +
                             (helper / 'popot_cleanup.go').read_bytes()).hexdigest()
    stamp = local / 'substrate-cleanup.json'
    try:
        installed = json.loads(stamp.read_text())
    except (FileNotFoundError, ValueError):
        installed = {}
    if not isinstance(installed, dict):
        installed = {}
    container = next(c for c in items[0]['spec']['template']['spec']['containers']
                     if c['name'] == 'atelet')
    name = items[0]['metadata']['name']
    desired_security = daemonset_patch(items[0], container['image'])[-1]['value']
    if (installed.get('version') == version and installed.get('image') == container['image']
            and container.get('securityContext') == desired_security):
        # Inspect the live DaemonSet rather than trusting a local stamp alone.
        run([*ds_base, 'rollout', 'status', 'daemonset/' + name, '--timeout=300s'])
        print('Local atelet cleanup fix is current; build skipped.', flush=True)
        install_runsc_cleanup(root, checkout, config, kubectl, env, run)
        return
    if source != updated:
        main_file.write_text(updated)
    for name in ('popot_cleanup.go', 'popot_cleanup_test.go'):
        main_file.with_name(name).write_text((helper / name).read_text())
    run(['go', 'test', 'popot_cleanup.go', 'popot_cleanup_test.go'], cwd=main_file.parent)
    arch = run(['go', 'env', 'GOARCH'], capture_output=True).stdout.strip()
    env['KO_DEFAULTPLATFORMS'] = 'linux/' + arch
    ldflags = '-X=github.com/agent-substrate/substrate/internal/version.Version=popot-cleanup-' + PIN[:12]
    image = run([str(ko), 'build', './cmd/atelet', '--ldflags=' + ldflags],
                cwd=checkout, stdout=subprocess.PIPE).stdout.strip()
    if not re.fullmatch(r'localhost:5001/[^\s]+@sha256:[0-9a-f]{64}', image):
        raise ValueError('Build did not return a pinned image from the local registry')
    name = items[0]['metadata']['name']
    ds = json.loads(run([*ds_base, 'get', 'daemonset', name, '-o', 'json'], capture_output=True).stdout)
    patch = daemonset_patch(ds, image)
    run([*ds_base, 'patch', 'daemonset', name, '--type=json', '-p', json.dumps(patch)])
    run([*ds_base, 'rollout', 'status', 'daemonset/' + name, '--timeout=300s'])
    stamp.write_text(json.dumps({'version': version, 'image': image}) + '\n')
    print('Local atelet cleanup fix deployed.', flush=True)
    install_runsc_cleanup(root, checkout, config, kubectl, env, run)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc) if isinstance(exc, ValueError)
                         else 'Cleanup setup failed; inspect the preceding build/deployment output')
