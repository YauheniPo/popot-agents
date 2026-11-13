"""Run frozen model/parameter variants sequentially and publish paired comparisons."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

from evals import save
from evals.compare import compare, load, render
from evals.settings import load_config, validate_config
from evals.names import experiment_name


ROOT = Path(__file__).resolve().parents[1]


def run(output, config, *, cases=(), repetitions=None):
    config = dict(config)
    if repetitions is not None:
        config['repetitions'] = repetitions
    validate_config(config)
    repetitions = config['repetitions']
    variants = config['variants']
    known = {c['id'] for c in json.loads((ROOT / 'evals/cases.json').read_text())}
    if set(cases) - known:
        raise ValueError('Unknown case')
    output.mkdir(parents=True, exist_ok=False)
    # Short names also become safe container paths; full details stay in manifest.
    prefix = datetime.now(timezone.utc).strftime('matrix-%Y%m%dT%H%M%S') + '-' + os.urandom(3).hex()
    run_id = prefix.removeprefix('matrix-')
    manifest = {'configuration': config, 'started_at': datetime.now(timezone.utc).isoformat(),
                'run_id': run_id, 'runs': {}, 'comparisons': {}}
    save(output / 'matrix.json', manifest)
    # Run the reference first even if the supplied variants list is reordered.
    variants = sorted(variants, key=lambda v: v['name'] != config['baseline'])
    for variant in variants:
        name = variant['name']
        command = ['bash', str(ROOT / 'evals/run-all.sh'), prefix + '-' + name,
                   '--model', variant['model'], '--parameters', json.dumps(variant['parameters']),
                   '--experiment', experiment_name(variant['model'], variant['parameters'], 'evals', run_id),
                   '--run-id', run_id]
        command += ['--repetitions', str(repetitions)]
        for case in cases:
            command += ['--case', case]
        env = {**os.environ, 'POPOT_EVAL_OUTPUT': str(output / name), 'POPOT_EVAL_SKIP_RUNTIME': '1',
               'POPOT_EVAL_BASELINE': '1' if name == config['baseline'] else '0'}
        subprocess.run(command, cwd=ROOT, env=env, check=True)
        manifest['runs'][name] = str(output / name / 'baseline')
        save(output / 'matrix.json', manifest)
        if name != config['baseline']:
            reference = load(output / config['baseline'] / 'baseline')
            candidate = load(output / name / 'baseline')
            result = compare(reference, candidate)
            from evals.metrics import publish_comparison
            result['monitoring'] = publish_comparison(result, reference['config'], candidate['config'])
            save(output / name / 'baseline/comparison.json', result)
            (output / name / 'baseline/comparison.md').write_text(render(result))
            manifest['comparisons'][name] = result
            save(output / 'matrix.json', manifest)
    manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
    save(output / 'matrix.json', manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variant', action='append', help='Select configured variants; include the baseline')
    parser.add_argument('--baseline', help='Override the configured baseline variant')
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--repetitions', type=int, choices=range(1, 4))
    args = parser.parse_args()
    config = load_config()
    if args.variant:
        if set(args.variant) - {v['name'] for v in config['variants']}:
            parser.error('Unknown variant')
        config['variants'] = [v for v in config['variants'] if v['name'] in args.variant]
    if args.baseline:
        config['baseline'] = args.baseline
    run(args.output.resolve(), config, cases=args.case, repetitions=args.repetitions)


if __name__ == '__main__':
    main()
