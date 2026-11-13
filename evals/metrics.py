"""Run-level observations and OTLP gauges for the existing local collector."""
from collections import Counter
import json
import hashlib
import math
import os
from pathlib import Path
import re
import select
import subprocess
import time
from urllib import request


# One source for Phoenix optimization directions, comparisons and Grafana signs.
METRICS = {
    'objective_score': ('MAXIMIZE', 'Проверенный успех', 'percentunit'),
    'functional_success': ('MAXIMIZE', 'Функциональные проверки', 'percentunit'),
    'judge_score': ('MAXIMIZE', 'Оценка judge', 'percentunit'),
    'self_score': ('NONE', 'Самооценка агента', 'percentunit'),
    'false_completion': ('MINIMIZE', 'Ложное завершение', 'percentunit'),
    'latency_seconds': ('MINIMIZE', 'Длительность', 's'),
    'total_tokens': ('MINIMIZE', 'Токены всего', 'short'),
    'prompt_tokens': ('MINIMIZE', 'Входные токены', 'short'),
    'completion_tokens': ('MINIMIZE', 'Выходные токены', 'short'),
    'llm_requests': ('MINIMIZE', 'LLM-запросы', 'short'),
    'steps': ('MINIMIZE', 'Шаги агента', 'short'),
    'model_retries': ('MINIMIZE', 'Повторы запросов', 'short'),
    'request_errors': ('MINIMIZE', 'Ошибки LLM-запросов', 'short'),
    'tool_calls': ('MINIMIZE', 'Вызовы tools', 'short'),
    'tool_errors': ('MINIMIZE', 'Ошибки tools', 'short'),
    'format_errors': ('MINIMIZE', 'Ошибки формата', 'short'),
    'coordination_turns': ('MINIMIZE', 'Coordination turns', 'short'),
    'judge_tokens': ('MINIMIZE', 'Токены judge отдельно', 'short'),
}


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def measure(row, events):
    counts = Counter(e.get('event') for e in events)
    requests = [e for e in events if e.get('event') == 'model_request']
    tools = {(e.get('agent_session_id'), e.get('call_id')) for e in events if e.get('event') == 'tool_call'}
    calls = row.get('gateway_calls')
    result = {'objective_score': float(row['passed']),
              'functional_success': float(row.get('functional_passed', False)),
              'false_completion': float(row.get('false_completion', False)),
              'latency_seconds': row['elapsed_seconds'],
              'total_tokens': row.get('tokens'),
              'llm_requests': len(calls) if calls is not None else len(requests),
              'steps': sum(e.get('attempt', 1) == 1 for e in requests),
              'model_retries': sum(e.get('attempt', 1) > 1 for e in requests),
              'request_errors': sum(counts[k] for k in ('model_timeout', 'model_http_error', 'model_transport_error')),
              'tool_calls': len(tools), 'tool_errors': counts['tool_error'],
              'format_errors': counts['final_result_invalid'],
              # Orchestrator and worker both announce a turn; only worker events carry its number.
              'coordination_turns': sum(e.get('event') == 'turn_started' and type(e.get('turn')) is int for e in events),
              'judge_score': row.get('judge', {}).get('score'),
              'self_score': row.get('self_assessment', {}).get('score'),
              'judge_tokens': row.get('judge', {}).get('tokens')}
    for source in ('prompt', 'completion'):
        values = [(c.get('usage') or {}).get(source + '_tokens') for c in calls or []]
        result[source + '_tokens'] = (sum(values) if row.get('usage_complete') is True
                                     and values and all(numeric(v) for v in values) else None)
    return {k: v if numeric(v) else None for k, v in result.items()}


def aggregate(rows):
    return {name: sum(values) / len(values) if values and all(numeric(v) for v in values) else None
            for name in METRICS for values in [[r.get('metrics', {}).get(name) for r in rows]]}


def labels(config):
    return {'series': config.get('suite_sha256', 'unknown')[:16],
            'experiment': config.get('experiment_name', 'unknown'),
            'model': config.get('primary_model', 'unknown'), 'harness': config.get('harness', 'unknown')}


def publish_report(report, config, *, phoenix_url='', display_name='', series_display_name=''):
    identity = labels(config)
    done, planned = len(report['results']), report.get('planned_runs', 0)
    result = emit(identity, {'completed_runs': done, 'planned_runs': planned,
                            'progress': done / planned if planned else 0}, kind='progress')
    complete = bool(report.get('finished_at')) and done == planned and planned > 0
    if complete:
        values = aggregate(report['results'])
        sent = emit(identity, values)
        result['ok'] &= sent['ok']
    if phoenix_url:
        sent = publish_info(config, phoenix_url=phoenix_url, display_name=display_name,
                            series_display_name=series_display_name)
        result['ok'] &= sent['ok']
    return result


def publish_info(config, *, phoenix_url, display_name='', series_display_name=''):
    identity = labels(config)
    params = json.dumps({k: v.get('model_parameters') for k, v in config.get('profiles', {}).items()}, sort_keys=True)
    info = {**identity, 'display_name': display_name or identity['experiment'],
            'parallel_models': str(config.get('evals', {}).get('parallel_models', 1)),
            'parameters': params, 'parameter_hash': hashlib.sha256(params.encode()).hexdigest()[:12],
            'source_revision': config.get('source_revision', 'unknown'), 'phoenix_url': phoenix_url}
    if series_display_name:
        info['series_display_name'] = series_display_name
    return emit(info, {'configuration': 1, 'display_name_updated_at_seconds': time.time()}, kind='info')


def publish_comparison(result, baseline, candidate):
    identity = {**labels(candidate), 'baseline': baseline['experiment_name']}
    first = emit(identity, {k: v['improvement'] for k, v in result['metrics'].items()}, kind='improvement')
    verdicts = {'worse': -1, 'same': 0, 'better': 1, 'tradeoff': 2, 'insufficient_data': 3}
    second = emit(identity, {'comparable': int(result['comparable']), 'verdict': verdicts[result['verdict']]}, kind='comparison')
    return {'ok': first['ok'] and second['ok']}


def payload(labels, values, *, kind='value'):
    points = []
    for name, value in values.items():
        if not numeric(value):
            continue
        attrs = {**labels, 'metric': name}
        points.append({'attributes': [{'key': k, 'value': {'stringValue': str(v)}} for k, v in attrs.items()],
                       'timeUnixNano': str(time.time_ns()), 'asDouble': value})
    return {'resourceMetrics': [{'resource': {'attributes': [
        {'key': 'service.name', 'value': {'stringValue': 'popot-evals'}}]},
        'scopeMetrics': [{'scope': {'name': 'popot.evals'}, 'metrics': [
            {'name': 'popot.eval.' + kind, 'gauge': {'dataPoints': points}}]}]}]}


def _post(body):
    """Let kubectl own credentials; expose only this OTLP path on loopback."""
    from ax_local.config import AX_CONFIG

    path = '/api/v1/namespaces/otel-system/services/http:opentelemetry-collector:4318/proxy/v1/metrics'
    command = ['kubectl', '--context=' + AX_CONFIG['context'], 'proxy', '--address=127.0.0.1',
               '--port=0', '--accept-paths=^' + path + '$']
    if not AX_CONFIG['context'].startswith('kind-'):
        raise ValueError('Eval monitoring requires the local kind cluster')
    env = dict(os.environ)
    kubeconfig = Path(__file__).resolve().parents[1] / 'ax_local/.local/kubeconfig'
    if 'KUBECONFIG' not in env and kubeconfig.is_file():
        env['KUBECONFIG'] = str(kubeconfig)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
    try:
        if not select.select([child.stdout], [], [], 8)[0]:
            raise TimeoutError('OTLP proxy startup timeout')
        match = re.search(r'127\.0\.0\.1:(\d+)', child.stdout.readline())
        if not match:
            raise RuntimeError('OTLP proxy did not report its port')
        call = request.Request('http://127.0.0.1:' + match[1] + path,
                               data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with request.build_opener(request.ProxyHandler({})).open(call, timeout=8) as response:
            result = json.load(response)
        if result.get('partialSuccess', {}).get('rejectedDataPoints', 0):
            raise RuntimeError('Collector rejected eval points')
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()


def emit(labels, values, *, kind='value'):
    try:
        _post(payload(labels, values, kind=kind))
        return {'ok': True}
    except (OSError, ValueError, RuntimeError) as exc:
        return {'ok': False, 'error_type': type(exc).__name__}


if __name__ == '__main__':
    import argparse
    from evals import save

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    report = json.loads((args.directory / 'report.json').read_text())
    config = json.loads((args.directory / 'config.json').read_text())
    journal = args.directory / 'phoenix.json'
    publication = json.loads(journal.read_text()) if journal.exists() else {}
    report['monitoring'] = publish_report(report, config, phoenix_url=publication.get('url', ''),
                                          display_name=publication.get('display_name', ''),
                                          series_display_name=publication.get('series_display_name', ''))
    save(args.directory / 'report.json', report)
    print('EVAL_MONITORING', json.dumps(report['monitoring']))
    raise SystemExit(0 if report['monitoring']['ok'] else 1)
