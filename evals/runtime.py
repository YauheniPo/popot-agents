"""Snapshot existing Phoenix traces and attach deterministic harness observations.

No agent execution or model judge. Unknown quality, cost and incomplete usage
stay unknown. CODE annotations describe runtime behavior, not task correctness.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from urllib import error, parse, request

from evals import save


VERSION = 'harness-baseline-v1'
ATTRIBUTES = {
    'session.id', 'popot.root_session_id', 'popot.parent_session_id',
    'popot.agent_session_id', 'popot.role', 'popot.agent_profile',
    'popot.requested_model', 'popot.outcome', 'popot.incomplete',
    'popot.attribution_missing', 'popot.error_type', 'tool.name',
    'popot.skills.configured', 'popot.skills.loaded',
    'llm.model_name', 'http.response.status_code',
    'llm.token_count.prompt', 'llm.token_count.completion', 'llm.token_count.total',
    'popot.telemetry.version', 'popot.harness.events_chunk', 'popot.worker_image',
    'popot.harness.sha256', 'popot.skills.prompt_sha256',
    'popot.input.available', 'popot.output.available',
    'popot.input.truncated', 'popot.output.truncated',
    'popot.input.bytes', 'popot.output.bytes',
}
LIMITATIONS = [
    'completed — заявление агента; качество задачи и false completion по этим spans неизвестны.',
    'Этот отчёт сохраняет только метаданные. В Phoenix v2 payload содержит запросы/ответы и tool IO после redaction; legacy spans могут не иметь содержимого.',
    'V2 фиксирует параметры запроса, хеш harness и загруженных skills; полный source/role provenance заморожен в controlled eval config, а не во всех рабочих traces.',
    'Нет подтверждённой стоимости, TTFT, причин fallback и отдельных budget/retry settings.',
    'Legacy AGENT хранит последние 64 события; v2 выносит предыдущие блоки в CHAIN Harness events. Окно и потерянные при аварии события всё ещё ограничивают счётчики.',
    'Срез по start_time содержит только уже экспортированные закрытые spans. Активные и потерянные при аварии spans могут отсутствовать.',
    'Отсутствующий parent/root в окне не доказывает потерю: он мог начаться вне окна или ещё выполняться.',
    'Разные рабочие задачи не образуют контролируемый A/B тест. Для сравнения качества нужен cases.json на одинаковой модели и бюджетах.',
]


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, endpoint, api_key, *, timeout_seconds=30):
        url = parse.urlsplit(endpoint)
        if (url.scheme not in {'http', 'https'} or not url.hostname or url.username
                or url.password or url.query or url.fragment or url.path not in {'', '/'}):
            raise ValueError('Phoenix endpoint must be an HTTP(S) origin without credentials')
        self.endpoint, self.api_key = endpoint.rstrip('/'), api_key
        self.timeout_seconds = timeout_seconds
        self.opener = request.build_opener(NoRedirect())

    def call(self, path, *, params=None, payload=None):
        url = self.endpoint + path + ('?' + parse.urlencode(params) if params else '')
        req = request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                              headers={'Authorization': 'Bearer ' + self.api_key,
                                       'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=self.timeout_seconds) as response:
                return json.load(response)
        except error.HTTPError as exc:
            # Error bodies can echo request attributes or credentials.
            status = exc.code
            exc.close()
            raise RuntimeError(f'Phoenix HTTP {status}') from None
        except error.URLError:
            raise RuntimeError('Phoenix connection failed') from None


def sanitize_span(span):
    attributes = {k: v for k, v in span.get('attributes', {}).items() if k in ATTRIBUTES}
    for side in ('input', 'output'):
        if side + '.value' in span.get('attributes', {}):
            attributes.setdefault('popot.' + side + '.available', True)
    metadata = span.get('attributes', {}).get('metadata', {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    if isinstance(metadata, dict):
        attributes['metadata'] = {k: v for k, v in metadata.items()
                                  if k in {'session_id', 'root_session_id', 'role'}}
    return {**{k: span.get(k) for k in ('context', 'name', 'span_kind', 'parent_id',
                                       'start_time', 'end_time', 'status_code')},
            'attributes': attributes,
            'events': [{'name': e['name']} for e in span.get('events', [])]}


def fetch_spans(api, project, start, end):
    params = {'limit': 1000, 'sort': 'id', 'order': 'asc', 'start_time': start, 'end_time': end}
    spans, cursors = {}, set()
    while True:
        page = api.call('/v1/projects/' + parse.quote(project, safe='') + '/spans', params=dict(params))
        for span in page['data']:
            cleaned = sanitize_span(span)
            spans[cleaned['context']['span_id']] = cleaned
        cursor = page['next_cursor']
        if not cursor:
            return list(spans.values())
        if cursor in cursors:
            raise ValueError('Phoenix repeated pagination cursor')
        cursors.add(cursor)
        params['cursor'] = cursor


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timezone required')
    return parsed.astimezone(timezone.utc)


def duration(span):
    try:
        seconds = (timestamp(span['end_time']) - timestamp(span['start_time'])).total_seconds()
        return seconds if seconds >= 0 else None
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def agent_id(span):
    attrs = span.get('attributes', {})
    metadata = attrs.get('metadata', {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    return attrs.get('popot.agent_session_id') or (metadata.get('session_id') if isinstance(metadata, dict) else None)


def valid_tokens(value):
    return type(value) is int and value >= 0


def evaluate_span(span):
    attrs, kind = span.get('attributes', {}), span['span_kind']
    root = attrs.get('popot.root_session_id')
    scores = {'harness.session_link_ok': int(bool(root) and attrs.get('session.id') == root),
              'harness.agent_identity_present': int(bool(agent_id(span)))}
    if span['status_code'] in {'OK', 'ERROR'}:
        scores['harness.runtime_ok'] = int(span['status_code'] == 'OK')
    elapsed = duration(span)
    if elapsed is not None:
        scores['harness.duration_seconds'] = elapsed
    if kind == 'LLM':
        usage = {k: attrs.get('llm.token_count.' + k) for k in ('prompt', 'completion', 'total')}
        scores['harness.usage_complete'] = int(all(valid_tokens(v) for v in usage.values()))
        scores['harness.model_identified'] = int(bool(attrs.get('llm.model_name')))
        for key, value in usage.items():
            if valid_tokens(value):
                scores['harness.tokens_' + key] = value
    elif kind == 'TOOL':
        scores['harness.tool_finished'] = int(attrs.get('popot.incomplete') is not True)
    elif kind == 'AGENT' and attrs.get('popot.outcome') in {'completed', 'failed', 'blocked', 'partial', 'interrupted'}:
        scores['harness.declared_completed'] = int(attrs['popot.outcome'] == 'completed')
    if kind in {'LLM', 'TOOL'} and attrs.get('popot.telemetry.version') == 'popot-telemetry-v2':
        for side in ('input', 'output'):
            scores['harness.' + side + '_captured'] = int(attrs.get('popot.' + side + '.available') is True)
            scores['harness.' + side + '_truncated'] = int(attrs.get('popot.' + side + '.truncated') is True)
    return scores


def distribution(values):
    values = sorted(v for v in values if v is not None)
    # Nearest-rank percentiles; sample count accompanies small-sample tails.
    return {'n': len(values), **{name: values[max(0, math.ceil(len(values) * p) - 1)] if values else None
                                for name, p in [('p50', .5), ('p95', .95), ('p99', .99)]}}


def metrics(spans):
    llm = [s for s in spans if s['span_kind'] == 'LLM']
    known = [s['attributes']['llm.token_count.total'] for s in llm
             if valid_tokens(s['attributes'].get('llm.token_count.total'))]
    statuses = Counter(s['status_code'] for s in spans)
    terminal = statuses['OK'] + statuses['ERROR']
    return {'spans': len(spans), 'statuses': dict(statuses),
            'runtime_error_rate': statuses['ERROR'] / terminal if terminal else None,
            'latency_seconds': distribution(duration(s) for s in spans),
            'llm_calls': len(llm), 'tokens_known': sum(known),
            'tokens': sum(known) if llm and len(known) == len(llm) else None,
            'llm_usage_coverage': sum(evaluate_span(s)['harness.usage_complete'] for s in llm) / len(llm) if llm else None}


def grouped(spans, key):
    groups = defaultdict(list)
    for span in spans:
        groups[key(span) or 'unknown'].append(span)
    return {name: metrics(rows) for name, rows in sorted(groups.items())}


def build_report(spans, provenance):
    agents = [s for s in spans if s['span_kind'] == 'AGENT']
    ids = {s['context']['span_id'] for s in spans}
    sessions = defaultdict(list)
    for span in spans:
        sessions[span['attributes'].get('popot.root_session_id') or 'unknown'].append(span)
    session_rows = []
    for root, rows in sorted(sessions.items()):
        times = [s for s in rows if duration(s) is not None]
        session_rows.append({'root_session_id': root,
            'agent_sessions': sorted({agent_id(s) for s in rows if agent_id(s)}),
            'phoenix_sessions': sorted({s['attributes']['session.id'] for s in rows if s['attributes'].get('session.id')}),
            'root_agent_observed': any(s['span_kind'] == 'AGENT' and agent_id(s) == root for s in rows),
            'observed_wall_seconds': (max(timestamp(s['end_time']) for s in times) -
                                      min(timestamp(s['start_time']) for s in times)).total_seconds() if times else None,
            **metrics(rows)})
    audit = {'missing_agent_session_id': sum(not s['attributes'].get('popot.agent_session_id') for s in spans),
             'legacy_agent_identity': sum(not s['attributes'].get('popot.agent_session_id') and bool(agent_id(s)) for s in spans),
             'session_link_mismatch': sum(not evaluate_span(s)['harness.session_link_ok'] for s in spans),
             'parent_outside_snapshot': sum(bool(s.get('parent_id')) and s['parent_id'] not in ids for s in spans),
             'invalid_duration': sum(duration(s) is None for s in spans),
             'event_tail_at_limit': sum(len(s.get('events', [])) >= 64 and
                 s['attributes'].get('popot.telemetry.version') != 'popot-telemetry-v2' for s in agents),
             'harness_event_chunks': sum(s['attributes'].get('popot.harness.events_chunk') is True for s in spans),
             **{kind.lower() + '_' + side + '_missing': sum(
                 s['attributes'].get('popot.' + side + '.available') is not True
                 for s in spans if s['span_kind'] == kind) for kind in ('LLM', 'TOOL') for side in ('input', 'output')},
             'truncated_payloads': sum(s['attributes'].get('popot.' + side + '.truncated') is True
                 for s in spans for side in ('input', 'output')),
             'incomplete_tools': sum(s['attributes'].get('popot.incomplete') is True for s in spans if s['span_kind'] == 'TOOL')}
    return {'evaluator_version': VERSION, 'provenance': provenance,
            'summary': {**metrics(spans), 'root_sessions': len(sessions),
                        'declared_outcomes': dict(Counter(s['attributes'].get('popot.outcome', 'unknown') for s in agents)),
                        'retained_event_counts': dict(Counter(e['name'] for s in spans
                            if s['span_kind'] == 'AGENT' or s['attributes'].get('popot.harness.events_chunk') is True
                            for e in s.get('events', []))),
                        'functional_success_rate': None, 'false_completion_rate': None, 'cost': None},
            'audit': audit, 'sessions': session_rows,
            'by_kind': grouped(spans, lambda s: s['span_kind']),
            'by_role': grouped(spans, lambda s: s['attributes'].get('popot.role')),
            'by_model': grouped([s for s in spans if s['span_kind'] == 'LLM'], lambda s: s['attributes'].get('llm.model_name')),
            'by_tool': grouped([s for s in spans if s['span_kind'] == 'TOOL'], lambda s: s['attributes'].get('tool.name') or s['name']),
            'limitations': LIMITATIONS}


def annotate(api, spans):
    annotations = [{'span_id': s['context']['span_id'], 'name': name, 'annotator_kind': 'CODE',
                    'identifier': VERSION, 'result': {'score': value},
                    'metadata': {'evaluator_version': VERSION, 'scope': 'runtime observation; not task correctness'}}
                   for s in spans for name, value in evaluate_span(s).items()]
    for offset in range(0, len(annotations), 200):
        batch = annotations[offset:offset + 200]
        result = api.call('/v1/span_annotations', params={'sync': 'true'}, payload={'data': batch})
        if len(result.get('data', [])) != len(batch):
            raise ValueError('Phoenix did not confirm all annotations')
    return len(annotations)


def render(report):
    summary = report['summary']
    lines = ['# Runtime baseline перед заменой harness', '',
             f"Окно: `{report['provenance'].get('start_time')}` — `{report['provenance'].get('end_time')}` (UTC).", '',
             f"Spans: **{summary['spans']}**; root sessions: **{summary['root_sessions']}**; "
             f"LLM calls: **{summary['llm_calls']}**; известные токены: **{summary['tokens_known']}**.", '',
             'Ошибки runtime и заявления completed не доказывают качество результата задачи.', '',
             '| Вид | Spans | ERROR | p50, с | p95, с | p99, с |',
             '|---|---:|---:|---:|---:|---:|']
    for name, row in report['by_kind'].items():
        latency = row['latency_seconds']
        lines.append(f"| {name} | {row['spans']} | {row['statuses'].get('ERROR', 0)} | "
                     f"{latency['p50']} | {latency['p95']} | {latency['p99']} |")
    lines += ['', '## Полнота данных', '']
    lines += [f'- `{key}`: **{value}**.' for key, value in report['audit'].items()]
    lines += ['', '## Ограничения', '', *['- ' + item for item in report['limitations']], '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:6006')
    parser.add_argument('--project', default='popot-runtime')
    parser.add_argument('--hours', type=float, default=12)
    parser.add_argument('--end', help='Timezone-aware ISO timestamp; defaults to now')
    parser.add_argument('--input', type=Path, help='Re-score a saved snapshot without fetching spans')
    parser.add_argument('--annotate', action='store_true', help='Write idempotent CODE scores to Phoenix')
    parser.add_argument('--api-key-env', default='PHOENIX_API_KEY')
    args = parser.parse_args()
    if not math.isfinite(args.hours) or args.hours <= 0:
        parser.error('--hours must be positive and finite')
    api = None
    if not args.input or args.annotate:
        key = os.environ.get(args.api_key_env)
        if not key:
            parser.error(f'Set {args.api_key_env}; do not put tokens in command arguments')
        api = API(args.endpoint, key)
    if args.input:
        snapshot = json.loads(args.input.read_text())
        snapshot['spans'] = [sanitize_span(s) for s in snapshot['spans']]
    else:
        end = timestamp(args.end) if args.end else datetime.now(timezone.utc)
        start = end - timedelta(hours=args.hours)
        snapshot = {'provenance': {'endpoint': args.endpoint, 'project': args.project,
                                  'start_time': start.isoformat(), 'end_time': end.isoformat(),
                                  'captured_at': datetime.now(timezone.utc).isoformat(),
                                  'evaluator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
                    'spans': fetch_spans(api, args.project, start.isoformat(), end.isoformat())}
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    save(args.output / 'snapshot.json', snapshot)
    provenance = {**snapshot['provenance'],
                  'snapshot_sha256': hashlib.sha256((args.output / 'snapshot.json').read_bytes()).hexdigest()}
    report = build_report(snapshot['spans'], provenance)
    save(args.output / 'report.json', report)
    (args.output / 'summary.md').write_text(render(report))
    if args.annotate:
        count = annotate(api, snapshot['spans'])
        save(args.output / 'annotations.json', {'evaluator_version': VERSION, 'annotations': count,
                                               'snapshot_sha256': provenance['snapshot_sha256']})
    print(json.dumps({'output': str(args.output), 'spans': report['summary']['spans'], 'audit': report['audit']}))


if __name__ == '__main__':
    main()
