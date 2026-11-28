"""Direction-aware observed differences over an identical set of case trials."""
import argparse
import json
from pathlib import Path

from evals import save
from evals.metrics import METRICS, aggregate, numeric


def parameters(config):
    return {'model': config.get('primary_model'), 'harness': config.get('harness'),
            'model_parameters': config.get('profiles'),
            'workflow': config.get('evals', config.get('baseline', {})).get('workflow'),
            'parallel_models': config.get('evals', {}).get('parallel_models', 1),
            'worker_image': config.get('worker_image'), 'source_revision': config.get('source_revision')}


def compare(baseline, candidate):
    left, right = baseline['report'], candidate['report']
    a, b = baseline['config'], candidate['config']
    reasons = []
    for field in ('suite_sha256', 'memory_mode'):
        if not a.get(field) or a.get(field) != b.get(field):
            reasons.append('different_or_unknown_' + field)
    keys = lambda report: {(r['case_id'], r['repetition']) for r in report['results']}
    if keys(left) != keys(right) or len(keys(left)) != len(left['results']) or len(keys(right)) != len(right['results']):
        reasons.append('different_case_trials')
    if not left['results'] or not right['results']:
        reasons.append('empty_capture')
    for report in (left, right):
        if not report.get('finished_at') or len(report['results']) != report.get('planned_runs'):
            reasons.append('incomplete_capture')
        if any(r.get('result', {}).get('runner_error') for r in report['results']):
            reasons.append('runner_error')
        if any(r.get('model_pin_verified') is not True for r in report['results']):
            reasons.append('model_pin_not_verified')
    before, after = aggregate(left['results']), aggregate(right['results'])
    values = {}
    for name, (direction, label, unit) in METRICS.items():
        x, y = before[name], after[name]
        known = not reasons and numeric(x) and numeric(y)
        if name in {'judge_score', 'judge_tokens'} and (not a.get('judge_fingerprint') or
                                                       a.get('judge_fingerprint') != b.get('judge_fingerprint')):
            known = False
        delta = y - x if known else None
        improvement = delta * (1 if direction == 'MAXIMIZE' else -1) if known and direction != 'NONE' else None
        assessment = 'unknown'
        if known:
            if direction == 'NONE':
                assessment = 'same' if abs(delta) < 1e-9 else 'changed'
            else:
                assessment = 'same' if abs(improvement) < 1e-9 else ('better' if improvement > 0 else 'worse')
        values[name] = {'direction': direction, 'label': label, 'unit': unit,
                        'baseline': x, 'candidate': y, 'delta': delta, 'assessment': assessment,
                        'improvement': improvement,
                        'improvement_percent': improvement / abs(x) * 100 if improvement is not None and x != 0 else None,
                        'samples': len(left['results'])}
    signs = {v['assessment'] for v in values.values()} - {'same', 'unknown', 'changed'}
    verdict = 'insufficient_data' if reasons else 'same'
    if not reasons and signs:
        verdict = 'tradeoff' if len(signs) > 1 else next(iter(signs))
    if values['objective_score']['assessment'] == 'unknown':
        verdict = 'insufficient_data'
    p, q = parameters(a), parameters(b)
    return {'comparable': not reasons, 'reasons': sorted(set(reasons)), 'verdict': verdict,
            'metrics': values, 'parameter_changes': {k: {'baseline': p[k], 'candidate': q[k]} for k in p if p[k] != q[k]},
            'note': 'Observed differences, not statistical significance. Lower work/token counts do not establish quality.'}


def load(directory):
    directory = Path(directory)
    return {name: json.loads((directory / (name + '.json')).read_text()) for name in ('config', 'report')}


def render(result):
    labels = {'better': '🟢 лучше', 'worse': '🔴 хуже', 'same': 'без изменений',
              'changed': 'изменилась самооценка', 'unknown': 'недостаточно данных'}
    lines = ['# Сравнение с baseline', '', 'Итог: **' + result['verdict'] + '**.', '',
             '| Метрика | Направление | Baseline | Кандидат | Δ | Изменение |',
             '|---|---|---:|---:|---:|---|']
    for metric in result['metrics'].values():
        lines.append('| ' + ' | '.join(str(v) if v is not None else 'неизвестно' for v in (
            metric['label'], {'MAXIMIZE': 'больше лучше', 'MINIMIZE': 'меньше лучше', 'NONE': 'самоотчёт'}[metric['direction']],
            metric['baseline'], metric['candidate'], metric['delta'], labels[metric['assessment']])) + ' |')
    lines += ['', 'Причины несопоставимости: ' + (', '.join(result['reasons']) or 'нет'), '', result['note'], '']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    a, b = load(args.baseline), load(args.candidate)
    result = compare(a, b)
    if args.publish:
        from evals.metrics import publish_comparison
        result['monitoring'] = publish_comparison(result, a['config'], b['config'])
    save(args.candidate / 'comparison.json', result)
    (args.candidate / 'comparison.md').write_text(render(result))
