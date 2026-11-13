"""Import observed baseline spans and scored runs into the pinned local Phoenix.

Uses Phoenix's installed SDK in its container. No model calls or task replay.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from popot_agents.telemetry import VERSION, payload_attributes


def identifier(value, width=16):
    return hashlib.sha256(value.encode()).hexdigest()[:width]


def build_spans(row, events, experiment, *, model_records=()):
    root_key = f"{experiment}/{row['case_id']}/{row['repetition']}"
    trace_id, root_id = identifier(root_key, 32), identifier(root_key)
    root_sid = row.get('session_id') or 'root'
    session_ids = {root_sid, *(e.get('agent_session_id') for e in events)}
    model_records = [r for r in model_records if r['attributes'].get('popot.agent_session_id') in session_ids]
    captured = {(r['attributes'].get('popot.agent_session_id'), r['attributes'].get('popot.request_id'))
                for r in model_records if r['attributes'].get('popot.request_id')}

    def ownership(sid):
        return {'session.id': root_sid, 'popot.root_session_id': root_sid,
                'popot.agent_session_id': sid, 'popot.telemetry.version': VERSION}
    start, end = row['started_at_ns'], row['finished_at_ns']
    spans = [{'id': root_id, 'trace': trace_id, 'parent': None,
              'name': row['case_id'] + '/' + str(row['repetition']), 'kind': 'CHAIN',
              'start': start, 'end': end, 'events': [], 'error': not row['passed'],
              'attributes': {'baseline.time_source': 'API observer timestamps; post-hoc import',
                             'baseline.case_id': row['case_id'], 'baseline.passed': row['passed'],
                             **ownership(root_sid), 'baseline.outcome': row.get('outcome', {}).get('status', 'unknown')}}]
    requests = [(e.get('agent_session_id') or root_sid, e.get('request_id'))
                for e in events if e.get('event') == 'model_request']
    spans[0]['attributes'].update({
        'baseline.model_requests_observed': len(requests),
        'baseline.model_payloads_captured': len(model_records),
        'baseline.model_payloads_missing': sum(r not in captured for r in requests)})
    spans[0]['attributes'].update(payload_attributes('output', {
        'answer': row.get('result', {}).get('answer'), 'outcome': row.get('outcome'),
        'checks': row.get('checks'), 'passed': row['passed']}))
    sessions = {}
    for event in events:
        sid = event.get('agent_session_id') or root_sid
        when = event.get('observed_at_ns', start)
        if sid not in sessions:
            sessions[sid] = {'id': identifier(root_key + '/session/' + sid), 'trace': trace_id,
                             'parent': None, 'name': event.get('agent_role') or sid, 'kind': 'AGENT',
                             'start': when, 'end': when, 'events': [], 'error': False,
                             'attributes': {**ownership(sid), 'baseline.time_source': 'API observer',
                                            'baseline.parent_session': event.get('parent_session_id') or ''}}
        session = sessions[sid]
        session['end'] = max(session['end'], when)
        if event.get('event') == 'skills_loaded':
            session['attributes']['popot.skills.loaded'] = event.get('skills', [])
            if event.get('prompt_sha256'):
                session['attributes']['popot.skills.prompt_sha256'] = event['prompt_sha256']
        if event.get('event') == 'harness_started':
            session['attributes']['popot.harness.sha256'] = event.get('harness_sha256', '')
            session['attributes']['popot.role.sha256'] = event.get('role_sha256', '')
            session['attributes']['popot.harness.config'] = payload_attributes('config', event.get('config', {}), limit=16384)['config.value']
        fields = {k: v for k, v in event.items() if k not in {'observed_at_ns', 'content', 'output', 'arguments'}
                  and isinstance(v, (str, int, float, bool))}
        detail = payload_attributes('event', {k: v for k, v in event.items()
            if k not in {'input', 'output', 'arguments', 'content'}}, limit=16384)
        fields['popot.event.value'] = detail['event.value']
        fields['popot.event.truncated'] = detail['popot.event.truncated']
        session['events'].append({'name': event.get('event', 'unknown'), 'time': when, 'attributes': fields})
        if event.get('event') == 'assistant_answer' and 'content' in event:
            session['attributes'].update(payload_attributes('output', event['content']))
    for sid, span in sessions.items():
        parent = sessions.get(span['attributes']['baseline.parent_session'])
        span['parent'] = parent['id'] if parent else root_id
        spans.append(span)
    for task in row.get('result', {}).get('saved_chat', {}).get('work', {}).get('tasks', []):
        span = sessions.get(task.get('sessionId'))
        if span is None:
            continue
        outcome = task.get('outcome') or {}
        state = task.get('executionState')
        span['attributes']['popot.execution_state'] = state or 'unknown'
        span['attributes']['popot.outcome'] = outcome.get('status', 'unknown')
        span['attributes'].update(payload_attributes('output', outcome))
        span['error'] = state in {'TASK_STATE_FAILED', 'failed'} or outcome.get('status') in {'failed', 'blocked'}
        for key, value in (task.get('failure') or {}).items():
            if key in {'code', 'stage', 'error_type'} and isinstance(value, str):
                span['attributes']['popot.failure.' + key] = value
        if task.get('reportedAt'):
            span['end'] = max(span['end'], int(datetime.fromisoformat(task['reportedAt']).timestamp() * 1e9))
    pending = {}

    def finish(key, when, *, incomplete=False):
        span = pending.pop(key)
        span['end'] = max(span['start'], when)
        if incomplete:
            span['error'] = True
            span['attributes']['baseline.incomplete'] = True
        spans.append(span)

    for index, event in enumerate(events):
        sid = event.get('agent_session_id') or root_sid
        kind = event.get('event')
        when = event.get('observed_at_ns', start)
        category = ('tool' if kind in {'tool_call', 'tool_result', 'tool_error'} else
                    'model' if kind in {'model_request', 'model_response', 'model_timeout', 'model_http_error', 'model_transport_error'} else
                    'mcp' if kind.startswith('mcp_discovery_') else None)
        if category is None:
            continue
        key = (sid, category, event.get('call_id') if category == 'tool' else event.get('server') if category == 'mcp' else None)
        if kind == 'tool_error' and key not in pending:
            pending[key] = {'id': identifier(root_key + '/rejected/' + str(index)), 'trace': trace_id,
                'parent': sessions[sid]['id'], 'name': event.get('name') or 'tool', 'kind': 'TOOL',
                'start': when, 'end': when, 'events': [], 'error': True,
                'attributes': {**ownership(sid), 'popot.call_started_missing': True,
                    **(payload_attributes('input', event['input']) if 'input' in event else {})}}
        if kind in {'tool_call', 'model_request', 'mcp_discovery_started'}:
            if key in pending:
                finish(key, when, incomplete=True)
            pending[key] = {'id': identifier(root_key + '/event/' + str(index)), 'trace': trace_id,
                            'parent': sessions[sid]['id'], 'name': event.get('name') or event.get('server') or 'model',
                            'kind': ('CHAIN' if row.get('gateway_calls') or model_records else 'LLM') if category == 'model' else 'TOOL',
                            'start': when, 'end': when,
                            'error': False, 'events': [],
                            'attributes': {**ownership(sid), 'baseline.time_source': 'API observer'}}
            if category == 'model' and event.get('request_id'):
                pending[key]['attributes']['popot.request_id'] = event['request_id']
            if category == 'mcp':
                pending[key]['attributes']['popot.phase'] = 'mcp_discovery'
            if category == 'tool' and 'input' in event:
                pending[key]['attributes'].update(payload_attributes('input', event['input']))
                for suffix in ('truncated', 'bytes', 'sha256'):
                    if 'input_' + suffix in event:
                        pending[key]['attributes']['popot.input.' + suffix] = event['input_' + suffix]
            for field in ('arguments', 'model'):
                if field in event:
                    value = json.dumps(event[field], ensure_ascii=False)
                    pending[key]['attributes'][field + '.preview'] = value[:4000]
                    if len(value) > 4000:
                        pending[key]['attributes'][field + '.truncated'] = True
        elif key in pending:
            if kind in {'tool_error', 'model_timeout', 'model_http_error', 'model_transport_error', 'mcp_discovery_retry', 'mcp_discovery_failed'}:
                pending[key]['error'] = True
                pending[key]['attributes']['error.type'] = event.get('error_type') or kind
            if kind == 'model_http_error':
                pending[key]['attributes']['http.response.status_code'] = event['status']
            if kind == 'tool_result' and 'output' in event:
                pending[key]['attributes'].update(payload_attributes('output', event['output']))
                pending[key]['attributes']['output.preview'] = str(event['output'])[:4000]
                pending[key]['attributes']['output.truncated'] = len(str(event['output'])) > 4000
                for suffix in ('truncated', 'bytes', 'sha256'):
                    if 'output_' + suffix in event:
                        pending[key]['attributes']['popot.output.' + suffix] = event['output_' + suffix]
            if kind != 'tool_error':
                finish(key, when)
    for key in list(pending):
        finish(key, end, incomplete=True)
    for record in model_records:
        attrs = dict(record['attributes'])
        sid, rid = attrs.get('popot.agent_session_id'), attrs.get('popot.request_id')
        receipts = [c for c in row.get('gateway_calls', []) if rid and
                    (c.get('session_id'), c.get('request_id')) == (sid, rid)]
        attrs.update(ownership(sid))
        if sid in sessions and 'input.value' not in sessions[sid]['attributes'] and attrs.get('input.value'):
            request_body = json.loads(attrs['input.value'])
            if isinstance(request_body, dict) and isinstance(request_body.get('messages'), list):
                sessions[sid]['attributes'].update(payload_attributes('input', [m for m in request_body['messages']
                    if isinstance(m, dict) and m.get('role') == 'user']))
        attrs['baseline.time_source'] = 'AX provider proxy; captured HTTP body'
        attrs['baseline.gateway_attempts'] = len(receipts)
        attrs['popot.usage.source'] = 'gateway_receipts' if receipts else 'provider_response'
        attrs['baseline.gateway_call_ids'] = [c['id'] for c in receipts]
        if receipts:
            for source, target in [('tokens', 'total'), ('prompt_tokens', 'prompt'), ('completion_tokens', 'completion')]:
                values = [c.get('tokens') if source == 'tokens' else (c.get('usage') or {}).get(source) for c in receipts]
                attrs.pop('llm.token_count.' + target, None)
                if all(type(v) is int and v >= 0 for v in values):
                    attrs['llm.token_count.' + target] = sum(values)
        spans.append({'id': identifier(root_key + '/capture/' + record['context']['span_id']),
            'trace': trace_id, 'parent': sessions[sid]['id'] if sid in sessions else root_id,
            'name': record['name'], 'kind': 'LLM',
            'start': int(datetime.fromisoformat(record['start_time']).timestamp() * 1e9),
            'end': int(datetime.fromisoformat(record['end_time']).timestamp() * 1e9),
            'attributes': attrs, 'events': [], 'error': record.get('status_code') == 'ERROR'})
    for call in row.get('gateway_calls', []):
        if call.get('request_id') and (call.get('session_id'), call['request_id']) in captured:
            continue
        if not call.get('started_at') or call.get('duration_ms') is None:
            spans[0]['attributes']['baseline.untimed_gateway_calls'] = spans[0]['attributes'].get('baseline.untimed_gateway_calls', 0) + 1
            continue
        started = int(datetime.fromisoformat(call['started_at'].replace('Z', '+00:00')).timestamp() * 1e9)
        sid = call.get('session_id') or root_sid
        attributes = {**ownership(sid), 'baseline.time_source': 'OmniRoute gateway artifact',
                      'popot.input.available': False, 'popot.output.available': False,
                      'llm.model_name': call.get('model') or 'unknown', 'baseline.gateway_call_id': call['id']}
        if call.get('request_id'):
            attributes['popot.request_id'] = call['request_id']
        if call.get('tokens') is not None:
            attributes['llm.token_count.total'] = call['tokens']
        for field, attr in (('prompt_tokens', 'llm.token_count.prompt'), ('completion_tokens', 'llm.token_count.completion')):
            if field in (call.get('usage') or {}):
                attributes[attr] = call['usage'][field]
        spans.append({'id': identifier(root_key + '/gateway/' + call['id']), 'trace': trace_id,
                      'parent': sessions[sid]['id'] if sid in sessions else root_id,
                      'name': call.get('model') or 'gateway request', 'kind': 'LLM',
                      'start': started, 'end': started + int(call['duration_ms'] * 1e6),
                      'attributes': attributes, 'events': [], 'error': call.get('error', False)})
    judged = row.get('judge', {})
    if judged.get('started_at_ns') and judged.get('finished_at_ns'):
        spans.append({'id': identifier(root_key + '/judge'),
                      'trace': identifier(root_key + '/judge', 32), 'parent': None,
                      'name': 'Independent judge', 'kind': 'LLM',
                      'start': judged['started_at_ns'], 'end': judged['finished_at_ns'],
                      'attributes': {'llm.model_name': judged.get('model') or judged['requested_model'],
                                     'popot.evaluation': 'judge_score',
                                     **({'llm.token_count.total': judged['tokens']} if judged.get('tokens') is not None else {})},
                      'events': [], 'error': judged.get('status') != 'ok'})
    return spans


def configure_metrics(key):
    from evals.metrics import METRICS
    from evals.runtime import API

    api = API('http://127.0.0.1:6006', key)
    configs, params = {}, {'limit': 100}
    cursors = set()
    while True:
        page = api.call('/v1/annotation_configs', params=params)
        configs.update((item['name'], item) for item in page['data'])
        cursor = page.get('next_cursor')
        if not cursor:
            break
        if cursor in cursors:
            raise ValueError('Phoenix annotation configuration cursor repeated')
        cursors.add(cursor)
        params['cursor'] = cursor
    for name, (direction, label, _) in METRICS.items():
        name = 'popot.eval.' + name
        data = {'name': name, 'type': 'CONTINUOUS', 'optimization_direction': direction,
                'description': 'Managed Popot metric: ' + label, 'lower_bound': 0}
        if name.endswith(('score', 'success', 'completion')):
            data['upper_bound'] = 1
        if name not in configs:
            api.call('/v1/annotation_configs', payload=data)
        elif configs[name].get('optimization_direction') != direction:
            # Native REST updates use PUT, unlike creation. Keep request transport explicit.
            from urllib.request import Request
            call = Request(api.endpoint + '/v1/annotation_configs/' + configs[name]['id'],
                data=json.dumps(data).encode(), method='PUT',
                headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
            with api.opener.open(call, timeout=30) as response:
                response.read()


def export(directory, *, baseline=False):
    # Import heavy SDKs only for export, not for offline scorer tests.
    from opentelemetry.trace import SpanContext, TraceFlags, Status, StatusCode
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from phoenix.client import Client
    from evals import save

    report = json.loads((directory / 'report.json').read_text())
    config = json.loads((directory / 'config.json').read_text())
    cases = config.get('cases') or json.loads((Path(__file__).parent / 'cases.json').read_text())
    fingerprint = hashlib.sha256((directory / 'config.json').read_bytes()).hexdigest()
    key = os.environ['PHOENIX_ADMIN_SECRET']
    client = Client(base_url='http://127.0.0.1:6006', api_key=key)
    if config.get('suite_sha256'):
        configure_metrics(key)
    journal_path = directory / 'phoenix.json'
    if journal_path.exists():
        journal = json.loads(journal_path.read_text())
        if journal['config_sha256'] != fingerprint:
            raise ValueError('Phoenix journal belongs to another configuration')
        dataset = client.datasets.get_dataset(dataset=journal['dataset_id'], version_id=journal['dataset_version_id'])
        experiment = client.experiments.get(experiment_id=journal['experiment_id'])
    else:
        case_hash = config.get('suite_sha256') or hashlib.sha256((Path(__file__).parent / 'cases.json').read_bytes()).hexdigest()
        dataset_name = 'popot-harness-local-' + case_hash[:12]
        existing = next((d for d in client.datasets.list() if d['name'] == dataset_name), None)
        dataset = client.datasets.get_dataset(dataset=existing['id']) if existing else client.datasets.create_dataset(
            name=dataset_name, inputs=[{'case_id': c['id'], 'role': c['role'], 'task': c['task'],
                                       'followup': c.get('followup')} for c in cases],
            outputs=[{'criterion': 'Objective artifact and runtime checks in evals.baseline.evaluate'} for c in cases],
            metadata=[{'case_id': c['id'], 'cases_sha256': case_hash} for c in cases])
        experiment = client.experiments.create(dataset_id=dataset.id, dataset_version_id=dataset.version_id,
            experiment_name=config.get('experiment_name', directory.name), repetitions=report.get('planned_repetitions', max(r['repetition'] for r in report['results'])),
            experiment_description=('Judge v2 re-evaluation of saved AX outputs; original execution preserved, no agent replay.'
                                    if config.get('evaluation') else
                                    'Real AX execution with objective checks, fixed independent judge and separate agent self-assessment.'),
            experiment_metadata={'source_revision': config['source_revision'], 'worker_image': config['worker_image'],
                                 'model': config['primary_model'], 'harness': config['harness'],
                                 'memory_mode': config.get('memory_mode', 'enabled'),
                                 'config_sha256': fingerprint, 'method': config['method'],
                                 'model_parameters': config.get('profiles'),
                                 'workflow': config.get('evals', config.get('baseline', {})).get('workflow'),
                                 'suite_sha256': config.get('suite_sha256'),
                                 'judge_fingerprint': config.get('judge_fingerprint'),
                                 'scoring': config.get('scoring'), 'evaluation': config.get('evaluation'),
                                 'telemetry_version': config.get('telemetry_version', 'legacy'),
                                 'payload_limit_bytes': config.get('payload_limit_bytes')})
        journal = {'config_sha256': fingerprint, 'dataset_id': dataset.id, 'dataset_version_id': dataset.version_id,
                   'experiment_id': experiment['id'], 'runs': {}}
        save(journal_path, journal)
    resource = Resource({'openinference.project.name': experiment['project_name'], 'service.name': 'popot-baseline'})
    exporter = OTLPSpanExporter(endpoint='http://127.0.0.1:6006/v1/traces', headers={'Authorization': 'Bearer ' + key})
    examples = {e['input']['case_id']: e['id'] for e in dataset.examples}
    capture_path = directory / 'model-spans.jsonl'
    model_records = [json.loads(line) for line in capture_path.read_text().splitlines()] if capture_path.exists() else []
    for row in report['results']:
        row_key = row['case_id'] + '/' + str(row['repetition'])
        records = build_spans(row, [json.loads(line) for line in (directory / row['trace_file']).read_text().splitlines()],
                              experiment['id'], model_records=model_records)
        contexts = {s['id']: SpanContext(int(s['trace'], 16), int(s['id'], 16), False, TraceFlags(1)) for s in records}
        spans = [ReadableSpan(name=s['name'], context=contexts[s['id']], parent=contexts.get(s['parent']),
                  resource=resource, attributes={**s['attributes'], 'openinference.span.kind': s['kind']},
                  events=[Event(e['name'], e['attributes'], e['time']) for e in s['events']],
                  start_time=s['start'], end_time=s['end'], status=Status(StatusCode.ERROR if s['error'] else StatusCode.OK),
                  instrumentation_scope=InstrumentationScope('popot.baseline', '1')) for s in records]
        if row_key in journal['runs']:
            continue
        if exporter.export(spans).name != 'SUCCESS':
            raise RuntimeError('Phoenix span export failed')
        run = client.experiments.log_run(experiment_id=experiment['id'], dataset_example_id=examples[row['case_id']],
            output={'session_id': row['session_id'], 'status': row['outcome'].get('status'),
                    'checks': row['checks'], 'answer': row['result'].get('answer'),
                    'tokens': row.get('tokens'), 'tokens_known': row.get('tokens_known'),
                    'usage_complete': row.get('usage_complete'), 'gateway_calls': row.get('gateway_calls', []),
                    'observed': row['observed'], 'runner_error': row['result'].get('runner_error')},
            start_time=datetime.fromtimestamp(row['started_at_ns'] / 1e9, timezone.utc),
            end_time=datetime.fromtimestamp(row['finished_at_ns'] / 1e9, timezone.utc),
            repetition_number=row['repetition'], trace_id=records[0]['trace'])
        for name, value in {'success': row['passed'], 'functional_success': row['functional_passed'],
                            'false_completion': row['false_completion'], **row['checks']}.items():
            client.experiments.log_evaluation(experiment_run_id=run['id'], name=name, score=float(value),
                                             label=str(value).lower(), trace_id=records[0]['trace'])
        for name, value in {'latency_seconds': row['elapsed_seconds'], 'total_tokens': row.get('tokens'),
                            'tokens_known': row.get('tokens_known')}.items():
            if value is not None:
                client.experiments.log_evaluation(experiment_run_id=run['id'], name=name, score=float(value), trace_id=records[0]['trace'])
        from evals.metrics import METRICS
        sources = {'judge_score': row.get('judge', {}), 'self_score': row.get('self_assessment', {})}
        for name, value in row.get('metrics', {}).items():
            if name not in METRICS:
                continue
            source = sources.get(name, {})
            judge_trace = next((s['trace'] for s in records if s['attributes'].get('popot.evaluation') == 'judge_score'), records[0]['trace'])
            client.experiments.log_evaluation(experiment_run_id=run['id'], name='popot.eval.' + name,
                annotator_kind='LLM' if name in {'judge_score', 'self_score'} else 'CODE',
                score=float(value) if value is not None else None,
                error='Measurement unavailable' if value is None else None,
                explanation=source.get('explanation'),
                metadata={'optimization_direction': METRICS[name][0], 'source': name,
                          'judge_fingerprint': config.get('judge_fingerprint'),
                          'measurement_status': source.get('status'), 'error_type': source.get('error_type')},
                trace_id=judge_trace if name == 'judge_score' else records[0]['trace'])
        journal['runs'][row_key] = {'run_id': run['id'], 'trace_id': records[0]['trace'], 'spans': len(spans)}
        save(journal_path, journal)
        print('PHOENIX_IMPORTED', row_key, 'spans', len(spans), flush=True)
    exporter.shutdown()
    from evals.names import rename, dataset_label
    from evals.phoenix_providers import API
    api = API('http://127.0.0.1:6006', key)
    journal['display_name'] = rename(api, config, experiment['id'])
    journal['series_display_name'] = dataset_label(api, dataset.id)
    if baseline:
        api.query(
            'mutation($id:ID!){setExperimentBaseline(experimentId:$id,baseline:true){experiment{id}}}',
            {'id': experiment['id']})
    journal['url'] = client.experiments.get_experiment_url(dataset.id, experiment['id'])
    save(journal_path, journal)
    print('PHOENIX_EXPERIMENT', journal['url'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    export(args.directory, baseline=args.baseline)
