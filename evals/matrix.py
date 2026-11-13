"""Run frozen model/parameter variants with bounded parallelism and publish paired comparisons."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import shutil

from evals import exclusive, save
from evals.compare import compare, load, render
from evals.settings import load_config, validate_config
from evals.names import experiment_name


ROOT = Path(__file__).resolve().parents[1]


def run(output, config, *, cases=(), repetitions=None, resume=False, parallel_models=None):
    config = dict(config)
    if repetitions is not None:
        config['repetitions'] = repetitions
    if parallel_models is not None:
        config['parallel_models'] = parallel_models
    validate_config(config)
    from ax_local.config import AX_CONFIG
    concurrency = min(config.get('parallel_models', 1), len(config['variants']))
    slots = concurrency * (AX_CONFIG['delegation']['max_depth'] + 1) + AX_CONFIG['warm_workers']
    if concurrency > 1 and slots > AX_CONFIG['max_workers']:
        raise ValueError(f'Parallel evals need {slots} worker slots including reserve; max_workers={AX_CONFIG["max_workers"]}')
    known = {c['id'] for c in json.loads((ROOT / 'evals/cases.json').read_text())}
    if set(cases) - known:
        raise ValueError('Unknown case')
    if not resume:
        output.mkdir(parents=True, exist_ok=False)
    with exclusive(output / '.matrix.lock'):
        return _run(output, config, cases=cases, resume=resume)


def _launch(job):
    with job['log'].open('a') as log:
        # Let the coordinator handle terminal cancellation and await cleanup.
        subprocess.run(job['command'], cwd=ROOT, env=job['env'], check=True,
                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def _cancel(jobs):
    errors = []
    for job in jobs:
        try:
            subprocess.run(['docker', 'exec', 'popot-ax-local-api-1', 'python', '-c',
                'import sys; from pathlib import Path; Path(sys.argv[1]).touch()', job['cancel_file']],
                check=True, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append({'variant': job['name'], 'error_type': type(exc).__name__})
    return errors


def _run(output, config, *, cases, resume):
    if resume:
        manifest = json.loads((output / 'matrix.json').read_text())
        previous = {**manifest['configuration'], 'parallel_models': manifest['configuration'].get('parallel_models', 1)}
        requested = {**config, 'parallel_models': config.get('parallel_models', 1)}
        if previous != requested or manifest.get('cases', []) != list(cases):
            raise ValueError('Resume configuration must match the saved matrix')
        run_id = manifest['run_id']
    else:
        run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + os.urandom(3).hex()
        manifest = {'configuration': config, 'started_at': datetime.now(timezone.utc).isoformat(),
                    'cases': list(cases), 'run_id': run_id, 'runs': {}, 'comparisons': {}}
    snapshot = output / '.launcher/evals'
    if not snapshot.exists():
        shutil.copytree(ROOT / 'evals', snapshot, ignore=shutil.ignore_patterns('__pycache__'))
        settings_path = snapshot / 'config.json'
        settings = json.loads(settings_path.read_text())
        settings['x-evals'] = config
        save(settings_path, settings)
    manifest.update(status='running', failures={}, active_variants=[])
    for key in ('failed_variant', 'exit_code', 'error_type', 'failed_at', 'active_variant', 'finished_at'):
        manifest.pop(key, None)
    save(output / 'matrix.json', manifest)
    variants = sorted(config['variants'], key=lambda v: v['name'] != config['baseline'])
    concurrency = min(config.get('parallel_models', 1), len(variants))
    jobs, errors, attempted = [], [], set()

    def failed(name, exc):
        detail = {'error_type': type(exc).__name__, 'exit_code': getattr(exc, 'returncode', None),
                  'failed_at': datetime.now(timezone.utc).isoformat()}
        manifest['failures'][name] = detail
        if not errors:
            manifest.update(failed_variant=name, **detail)
        errors.append(exc)

    for variant in variants:
        name = variant['name']
        if name in manifest['runs']:
            continue
        directory = output / name / 'baseline'
        cancel = '/tmp/matrix-' + run_id + '-' + name + '-' + os.urandom(3).hex() + '.cancel'
        command = ['bash', str(snapshot / 'run-all.sh'), 'matrix-' + run_id + '-' + name,
                   '--model', variant['model'], '--parameters', json.dumps(variant['parameters']),
                   '--experiment', experiment_name(variant['model'], variant['parameters'], 'evals', run_id),
                   '--run-id', run_id, '--repetitions', str(config['repetitions']),
                   '--matrix-parallelism', str(concurrency), '--cancel-file', cancel]
        for case in cases:
            command += ['--case', case]
        if resume and (output / name).exists():
            report_path = directory / 'report.json'
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
            if (not report.get('finished_at') or not report.get('planned_runs')
                    or len(report.get('results', [])) != report['planned_runs']):
                exc = ValueError('Saved capture is incomplete; refusing to repeat agents automatically')
                failed(name, exc)
                manifest['status'] = 'failed'
                save(output / 'matrix.json', manifest)
                raise exc
            command = ['bash', str(snapshot / 'publish.sh'), str(directory)]
            if name == config['baseline']:
                command.append('--baseline')
        env = {**os.environ, 'POPOT_EVAL_REPO_ROOT': str(ROOT),
               'POPOT_EVAL_LAUNCHER_ROOT': str(snapshot), 'POPOT_EVAL_OUTPUT': str(output / name), 'POPOT_EVAL_SKIP_RUNTIME': '1',
               'POPOT_EVAL_BASELINE': '1' if name == config['baseline'] else '0'}
        jobs.append({'name': name, 'command': command, 'env': env, 'cancel_file': cancel,
                     'log': output / (name + '.log')})

    def comparisons():
        if config['baseline'] not in manifest['runs']:
            return
        for variant in variants:
            name = variant['name']
            if name == config['baseline'] or name not in manifest['runs'] or name in manifest['comparisons'] or name in attempted:
                continue
            attempted.add(name)
            try:
                reference = load(output / config['baseline'] / 'baseline')
                candidate = load(output / name / 'baseline')
                result = compare(reference, candidate)
                from evals.metrics import publish_comparison
                result['monitoring'] = publish_comparison(result, reference['config'], candidate['config'])
                save(output / name / 'baseline/comparison.json', result)
                (output / name / 'baseline/comparison.md').write_text(render(result))
                if not result['monitoring']['ok']:
                    raise RuntimeError('Eval comparison publication failed; saved measurements can be resumed')
                manifest['comparisons'][name] = result
            except Exception as exc:
                failed(name, exc)

    pending, queue = {}, iter(jobs)
    executor = ThreadPoolExecutor(max_workers=concurrency)

    def submit():
        for job in queue:
            pending[executor.submit(_launch, job)] = job
            print('MATRIX_START', job['name'], 'log', job['log'], flush=True)
            break
        manifest['active_variants'] = [j['name'] for j in pending.values()]
        if len(pending) == 1:
            manifest['active_variant'] = manifest['active_variants'][0]
        else:
            manifest.pop('active_variant', None)
        save(output / 'matrix.json', manifest)

    try:
        for _ in range(concurrency):
            submit()
        comparisons()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                job = pending.pop(future)
                try:
                    future.result()
                    manifest['runs'][job['name']] = str(output / job['name'] / 'baseline')
                    print('MATRIX_DONE', job['name'], flush=True)
                except Exception as exc:
                    failed(job['name'], exc)
                    print('MATRIX_FAILED', job['name'], type(exc).__name__, flush=True)
                comparisons()
                submit()
    except KeyboardInterrupt:
        manifest['cancel_errors'] = _cancel(list(pending.values()))
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        for future, job in pending.items():
            if not future.cancelled() and future.exception() is None:
                manifest['runs'][job['name']] = str(output / job['name'] / 'baseline')
        manifest.update(status='cancelled', cancelled_at=datetime.now(timezone.utc).isoformat(), active_variants=[])
        manifest.pop('active_variant', None)
        save(output / 'matrix.json', manifest)
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    manifest.update(status='failed' if errors else 'completed', active_variants=[])
    manifest.pop('active_variant', None)
    manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
    save(output / 'matrix.json', manifest)
    if errors:
        raise errors[0]
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variant', action='append', help='Select configured variants; include the baseline')
    parser.add_argument('--baseline', help='Override the configured baseline variant')
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--repetitions', type=int, choices=range(1, 4))
    parser.add_argument('--parallel-models', type=int, choices=range(1, 6), help='Maximum simultaneously measured model variants')
    parser.add_argument('--resume', action='store_true', help='Resume saved publication; never repeat an incomplete capture')
    args = parser.parse_args()
    config = load_config()
    if args.variant:
        if set(args.variant) - {v['name'] for v in config['variants']}:
            parser.error('Unknown variant')
        config['variants'] = [v for v in config['variants'] if v['name'] in args.variant]
    if args.baseline:
        config['baseline'] = args.baseline
    run(args.output.resolve(), config, cases=args.case, repetitions=args.repetitions, resume=args.resume,
        parallel_models=args.parallel_models)


if __name__ == '__main__':
    main()
