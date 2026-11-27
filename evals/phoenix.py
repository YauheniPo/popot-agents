"""Import observed baseline spans and scored runs into the pinned local Phoenix.

Uses Phoenix's installed SDK in its container. No model calls or task replay.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path


def identifier(value, width=16):
    return hashlib.sha256(value.encode()).hexdigest()[:width]


def build_spans(row, events, experiment):
    root_key = f"{experiment}/{row['case_id']}/{row['repetition']}"
    trace_id, root_id = identifier(root_key, 32), identifier(root_key)
    root_sid = row.get('session_id') or 'root'
    start, end = row['started_at_ns'], row['finished_at_ns']
    spans = [{'id': root_id, 'trace': trace_id, 'parent': None,
              'name': row['case_id'] + '/' + str(row['repetition']), 'kind': 'CHAIN',
              'start': start, 'end': end, 'events': [], 'error': not row['passed'],
              'attributes': {'baseline.time_source': 'API observer timestamps; post-hoc import',
                             'baseline.case_id': row['case_id'], 'baseline.passed': row['passed'],
                             'session.id': root_sid, 'baseline.outcome': row.get('outcome', {}).get('status', 'unknown')}}]
    sessions = {}
    for event in events:
        sid = event.get('agent_session_id') or root_sid
        when = event.get('observed_at_ns', start)
        if sid not in sessions:
            sessions[sid] = {'id': identifier(root_key + '/session/' + sid), 'trace': trace_id,
                             'parent': None, 'name': event.get('agent_role') or sid, 'kind': 'AGENT',
                             'start': when, 'end': when, 'events': [], 'error': False,
                             'attributes': {'session.id': sid, 'baseline.time_source': 'API observer',
                                            'baseline.parent_session': event.get('parent_session_id') or ''}}
        session = sessions[sid]
        session['end'] = max(session['end'], when)
        fields = {k: v for k, v in event.items() if k not in {'observed_at_ns', 'content', 'output', 'arguments'}
                  and isinstance(v, (str, int, float, bool))}
        session['events'].append({'name': event.get('event', 'unknown'), 'time': when, 'attributes': fields})
    for sid, span in sessions.items():
        parent = sessions.get(span['attributes']['baseline.parent_session'])
        span['parent'] = parent['id'] if parent else root_id
        spans.append(span)
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
        if kind in {'tool_call', 'model_request', 'mcp_discovery_started'}:
            if key in pending:
                finish(key, when, incomplete=True)
            pending[key] = {'id': identifier(root_key + '/event/' + str(index)), 'trace': trace_id,
                            'parent': sessions[sid]['id'], 'name': event.get('name') or event.get('server') or 'model',
                            'kind': ('CHAIN' if row.get('gateway_calls') else 'LLM') if category == 'model' else 'TOOL',
                            'start': when, 'end': when,
                            'error': False, 'events': [],
                            'attributes': {'session.id': sid, 'baseline.time_source': 'API observer'}}
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
            if kind == 'tool_result' and isinstance(event.get('output'), str):
                pending[key]['attributes']['output.preview'] = event['output'][:4000]
                pending[key]['attributes']['output.truncated'] = len(event['output']) > 4000
            if kind != 'tool_error':
                finish(key, when)
    for key in list(pending):
        finish(key, end, incomplete=True)
    for call in row.get('gateway_calls', []):
        if not call.get('started_at') or call.get('duration_ms') is None:
            spans[0]['attributes']['baseline.untimed_gateway_calls'] = spans[0]['attributes'].get('baseline.untimed_gateway_calls', 0) + 1
            continue
        started = int(datetime.fromisoformat(call['started_at'].replace('Z', '+00:00')).timestamp() * 1e9)
        sid = call.get('session_id') or root_sid
        attributes = {'session.id': sid, 'baseline.time_source': 'OmniRoute gateway artifact',
                      'llm.model_name': call.get('model') or 'unknown', 'baseline.gateway_call_id': call['id']}
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
    return spans


def export(directory):
    # Import heavy SDKs only for export, not for offline scorer tests.
    from opentelemetry.trace import SpanContext, TraceFlags, Status, StatusCode
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from phoenix.client import Client
    from evals import save

    report = json.loads((directory / 'report.json').read_text())
    if not report['results']:
        raise ValueError('Baseline has no measured runs to export')
    config = json.loads((directory / 'config.json').read_text())
    cases = json.loads((Path(__file__).parent / 'cases.json').read_text())
    fingerprint = hashlib.sha256((directory / 'config.json').read_bytes()).hexdigest()
    key = os.environ['PHOENIX_ADMIN_SECRET']
    client = Client(base_url='http://127.0.0.1:6006', api_key=key)
    journal_path = directory / 'phoenix.json'
    if journal_path.exists():
        journal = json.loads(journal_path.read_text())
        if journal['config_sha256'] != fingerprint:
            raise ValueError('Phoenix journal belongs to another configuration')
        dataset = client.datasets.get_dataset(dataset=journal['dataset_id'], version_id=journal['dataset_version_id'])
        experiment = client.experiments.get(experiment_id=journal['experiment_id'])
    else:
        case_hash = hashlib.sha256((Path(__file__).parent / 'cases.json').read_bytes()).hexdigest()
        dataset_name = 'popot-harness-local-' + case_hash[:12]
        existing = next((d for d in client.datasets.list() if d['name'] == dataset_name), None)
        dataset = client.datasets.get_dataset(dataset=existing['id']) if existing else client.datasets.create_dataset(
            name=dataset_name, inputs=[{'case_id': c['id'], 'role': c['role'], 'task': c['task'],
                                       'followup': c.get('followup')} for c in cases],
            outputs=[{'criterion': 'Objective artifact and runtime checks in evals.baseline.evaluate'} for c in cases],
            metadata=[{'case_id': c['id'], 'cases_sha256': case_hash} for c in cases])
        experiment = client.experiments.create(dataset_id=dataset.id, dataset_version_id=dataset.version_id,
            experiment_name=directory.name, repetitions=report.get('planned_repetitions', max(r['repetition'] for r in report['results'])),
            experiment_description='Real AX execution imported with original observed timestamps. Controlled local toolset; no model judge.',
            experiment_metadata={'source_revision': config['source_revision'], 'worker_image': config['worker_image'],
                                 'model': config['primary_model'], 'harness': config['harness'],
                                 'memory_mode': config.get('memory_mode', 'enabled'),
                                 'config_sha256': fingerprint, 'method': config['method']})
        journal = {'config_sha256': fingerprint, 'dataset_id': dataset.id, 'dataset_version_id': dataset.version_id,
                   'experiment_id': experiment['id'], 'runs': {}}
        save(journal_path, journal)
    resource = Resource({'openinference.project.name': experiment['project_name'], 'service.name': 'popot-baseline'})
    exporter = OTLPSpanExporter(endpoint='http://127.0.0.1:6006/v1/traces', headers={'Authorization': 'Bearer ' + key})
    examples = {e['input']['case_id']: e['id'] for e in dataset.examples}
    for row in report['results']:
        row_key = row['case_id'] + '/' + str(row['repetition'])
        records = build_spans(row, [json.loads(line) for line in (directory / row['trace_file']).read_text().splitlines()], experiment['id'])
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
        journal['runs'][row_key] = {'run_id': run['id'], 'trace_id': records[0]['trace'], 'spans': len(spans)}
        save(journal_path, journal)
        print('PHOENIX_IMPORTED', row_key, 'spans', len(spans), flush=True)
    exporter.shutdown()
    journal['url'] = client.experiments.get_experiment_url(dataset.id, experiment['id'])
    save(journal_path, journal)
    print('PHOENIX_EXPERIMENT', journal['url'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    export(parser.parse_args().directory)
