"""Re-evaluate saved outputs with the current judge in a separate experiment copy."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

from evals import save, scoring
from evals.metrics import measure


def regrade(source, output):
    config = json.loads((source / 'config.json').read_text())
    report = json.loads((source / 'report.json').read_text())
    if not report.get('finished_at'):
        raise ValueError('Regrade only a finished capture')
    output.mkdir(parents=True, exist_ok=False)
    config = deepcopy(config)
    config['experiment_name'] = config['experiment_name'] + '-judge-v2'
    config['evaluation'] = {'version': scoring.VERSION,
        'source_report_sha256': hashlib.sha256((source / 'report.json').read_bytes()).hexdigest(),
        'source_config_sha256': hashlib.sha256((source / 'config.json').read_bytes()).hexdigest(),
        'source_experiment': json.loads((source / 'phoenix.json').read_text()) if (source / 'phoenix.json').exists() else None,
        'evaluator_sha256': hashlib.sha256(Path(scoring.__file__).read_bytes()).hexdigest(),
        'self_assessment': 'Preserved from the original execution; not re-created by this judge'}
    config['judge_fingerprint'] = scoring.fingerprint(scoring.settings())
    config['scoring'] = scoring.settings()
    save(output / 'config.json', config)
    model_spans = source / 'model-spans.jsonl'
    if model_spans.is_file():
        destination = output / model_spans.name
        destination.touch(mode=0o600, exist_ok=False)
        shutil.copyfile(model_spans, destination)
    rows, report['results'] = report['results'], []
    report['source_finished_at'] = report.pop('finished_at')
    report.setdefault('planned_runs', len(rows))
    report['regraded_at'] = datetime.now(timezone.utc).isoformat()
    save(output / 'report.json', report)
    for row in rows:
        trace = Path(row['trace_file'])
        if trace.name != row['trace_file']:
            raise ValueError('Trace must be a local capture filename')
        shutil.copyfile(source / trace, output / trace)
        case = next(c for c in config['cases'] if c['id'] == row['case_id'])
        row['judge'] = scoring.evaluate_judge(row, case, config['scoring'])
        row['metrics'] = measure(row, [json.loads(line) for line in (output / trace).read_text().splitlines()])
        report['results'].append(row)
        save(output / (trace.stem + '.json'), row)
        save(output / 'report.json', report)
        print('REGRADED', row['case_id'], row['repetition'], row['judge'].get('status'), flush=True)
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save(output / 'report.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    regrade(args.source, args.output)
