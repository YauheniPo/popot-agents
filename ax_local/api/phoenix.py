"""Export runtime metadata through Phoenix's native spans API, with a disk outbox."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import threading
from urllib import error, parse, request
import uuid

from popot_agents.progress import redact_text
from popot_agents.telemetry import (VERSION, decode_payload, message_attributes,
                                    payload_attributes)


CONFIG_PATH = Path(__file__).resolve().parents[2] / 'evals/config.json'


def load_config(path=CONFIG_PATH):
    settings = json.loads(Path(path).read_text())['x-tracing']
    if not isinstance(settings, dict) or set(settings) != {
            'enabled', 'project', 'endpoint', 'outbox_directory',
            'request_timeout_seconds', 'retry_seconds', 'api_key_env'}:
        raise ValueError('Phoenix tracing config has invalid fields')
    if type(settings['enabled']) is not bool:
        raise ValueError('Phoenix tracing enabled must be boolean')
    if not isinstance(settings['project'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', settings['project']):
        raise ValueError('Phoenix tracing project must be a project name')
    if not isinstance(settings['endpoint'], str):
        raise ValueError('Phoenix endpoint must be an HTTP(S) origin')
    url = parse.urlsplit(settings['endpoint'])
    if (url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in {'', '/'}):
        raise ValueError('Phoenix endpoint must be an HTTP(S) origin without credentials')
    if not isinstance(settings['outbox_directory'], str) or not Path(settings['outbox_directory']).is_absolute():
        raise ValueError('Phoenix outbox_directory must be absolute')
    if not isinstance(settings['api_key_env'], str) or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', settings['api_key_env']):
        raise ValueError('Phoenix api_key_env must name an environment variable')
    for key in ('request_timeout_seconds', 'retry_seconds'):
        if type(settings[key]) not in (int, float) or not 0 < settings[key] <= 60:
            raise ValueError(f'Phoenix {key} must be between 0 and 60 seconds')
    return settings


def _now():
    return datetime.now(timezone.utc).isoformat()


def _warning(exc):
    print(json.dumps({'event': 'phoenix_export_failed', 'error_type': type(exc).__name__,
                      **({'http_status': exc.code} if isinstance(exc, error.HTTPError) else {})}),
          file=sys.stderr, flush=True)


class Span:
    def __init__(self, tracer, name, kind, *, parent=None, attributes=None):
        self.tracer = tracer
        self.data = {'name': redact_text(str(name))[:256], 'span_kind': kind,
            'context': {'trace_id': parent.data['context']['trace_id'] if parent else uuid.uuid4().hex,
                        'span_id': uuid.uuid4().hex[:16]},
            'parent_id': parent.data['context']['span_id'] if parent else None,
            'start_time': _now(), 'status_code': 'OK', 'attributes': dict(attributes or {})}
        self.finished = False
        self.parent = parent

    def payload(self, side, value):
        value = decode_payload(value)
        self.data['attributes'].update(payload_attributes(side, value))

    def model_input(self, body, *, request_id=None):
        self.payload('input', body)
        attrs = self.data['attributes']
        if isinstance(request_id, str) and re.fullmatch(r'[a-f0-9]{32}', request_id):
            attrs['popot.request_id'] = request_id
        clean = json.loads(attrs['input.value'])
        if isinstance(clean, dict) and not attrs['popot.input.truncated']:
            attrs.update(message_attributes('input', clean.get('messages')))
            messages = clean.get('messages')
            if (isinstance(messages, list) and self.parent and not self.parent.finished
                    and 'input.value' not in self.parent.data['attributes']):
                self.parent.payload('input', [m for m in messages
                    if isinstance(m, dict) and m.get('role') == 'user'])
            attrs['llm.invocation_parameters'] = json.dumps({k: v for k, v in clean.items()
                if k not in {'messages', 'tools', 'model'}}, ensure_ascii=False)
            tools = clean.get('tools')
            for index, tool in enumerate(tools[:16] if isinstance(tools, list) else []):
                attrs[f'llm.tools.{index}.tool.json_schema'] = json.dumps(tool, ensure_ascii=False)[:16000]
        attrs['popot.input.available'] = True

    def upstream_error(self, value):
        self.payload('error', value)

    def finish(self):
        if not self.finished:
            self.finished = True
            self.data['end_time'] = _now()
            self.tracer.submit(self.data)

    def model_result(self, status, body):
        self.payload('output', body)
        try:
            response = json.loads(body)
        except (ValueError, UnicodeError):
            response = {}
        if not isinstance(response, dict):
            response = {}
        self.data['attributes']['http.response.status_code'] = status
        if status >= 400 or not isinstance(response.get('choices'), list):
            self.data['status_code'] = 'ERROR'
        if isinstance(response.get('model'), str):
            self.data['attributes']['llm.model_name'] = redact_text(response['model'])[:256]
        usage = response.get('usage')
        if isinstance(usage, dict):
            for source, target in [('prompt_tokens', 'prompt'), ('completion_tokens', 'completion'),
                                   ('total_tokens', 'total')]:
                value = usage.get(source)
                if type(value) is int and value >= 0:
                    self.data['attributes']['llm.token_count.' + target] = value
            for source, field, target in [('prompt_tokens_details', 'cached_tokens', 'prompt_details.cache_read'),
                                          ('completion_tokens_details', 'reasoning_tokens', 'completion_details.reasoning')]:
                details = usage.get(source)
                value = details.get(field) if isinstance(details, dict) else None
                if type(value) is int and value >= 0:
                    self.data['attributes']['llm.token_count.' + target] = value
        clean = json.loads(self.data['attributes']['output.value'])
        if isinstance(clean, dict) and not self.data['attributes']['popot.output.truncated']:
            choices = clean.get('choices') if isinstance(clean.get('choices'), list) else []
            self.data['attributes'].update(message_attributes('output',
                [item['message'] for item in choices if isinstance(item, dict) and isinstance(item.get('message'), dict)]))
            if choices and isinstance(choices[0], dict) and isinstance(choices[0].get('finish_reason'), str):
                self.data['attributes']['llm.finish_reason'] = choices[0]['finish_reason']
        self.data['attributes']['popot.output.available'] = bool(body and body != b'{}')
        self.finish()


class AgentSpan(Span):
    def __init__(self, tracer, session_id, role, config, parent):
        delegation = config.get('_ax_delegation', {})
        root_id = (parent.data['attributes']['popot.root_session_id'] if parent
                   else delegation.get('root_session_id') or session_id)
        metadata = {'role': role, 'session_id': session_id, 'root_session_id': root_id,
                    'skills_configured': config.get('skills', [])}
        attributes = {'session.id': root_id, 'popot.role': role,
                      'popot.agent_session_id': session_id,
                      'popot.root_session_id': root_id,
                      'popot.agent_profile': config.get('agent', 'unknown'),
                      'popot.skills.configured': list(config.get('skills', [])),
                      'popot.telemetry.version': VERSION,
                      'popot.worker_image': os.getenv('AX_LOCAL_WORKER_IMAGE', 'unknown'),
                      'metadata': json.dumps(metadata)}
        parent_id = parent.session_id if parent else delegation.get('parent_session_id')
        if parent_id:
            attributes['popot.parent_session_id'] = parent_id
        super().__init__(tracer, role, 'AGENT', parent=parent, attributes=attributes)
        self.session_id = session_id
        self.pending = {}
        self.outcome = None

    def event(self, message):
        try:
            event = json.loads(message)
        except (TypeError, ValueError):
            return
        if not isinstance(event, dict) or event.get('agent_session_id', self.session_id) != self.session_id:
            return
        kind = event.get('event')
        if not isinstance(kind, str):
            return
        if kind not in {'output', 'assistant_reasoning', 'assistant_message', 'assistant_answer',
                        'tool_call', 'tool_result', 'tool_error'}:
            detail = payload_attributes('event', {k: v for k, v in event.items() if k != 'error'}, limit=16384)
            fields = {'popot.event.value': detail['event.value'],
                      'popot.event.truncated': detail['popot.event.truncated']}
            events = self.data.setdefault('events', [])
            if len(events) == 64:
                chunk = Span(self.tracer, 'Harness events', 'CHAIN', parent=self,
                    attributes={**self.base_attributes(), 'popot.harness.events_chunk': True})
                chunk.data['start_time'] = events[0]['timestamp']
                chunk.data['events'] = list(events)
                chunk.finish()
                events.clear()
            events.append({'name': redact_text(kind)[:160], 'timestamp': _now(), 'attributes': fields})
        if kind == 'harness_started':
            self.data['attributes']['popot.harness.config'] = payload_attributes('config', event.get('config', {}), limit=16384)['config.value']
            self.data['attributes']['popot.harness.sha256'] = event.get('harness_sha256', '')
            self.data['attributes']['popot.role.sha256'] = event.get('role_sha256', '')
        if kind == 'skills_loaded':
            configured = self.data['attributes']['popot.skills.configured']
            loaded = event.get('skills')
            if isinstance(loaded, list):
                self.data['attributes']['popot.skills.loaded'] = [s for s in loaded if s in configured]
                metadata = json.loads(self.data['attributes']['metadata'])
                metadata['skills_loaded'] = self.data['attributes']['popot.skills.loaded']
                self.data['attributes']['metadata'] = json.dumps(metadata)
            if isinstance(event.get('prompt_sha256'), str):
                self.data['attributes']['popot.skills.prompt_sha256'] = event['prompt_sha256']
        elif kind == 'tool_call':
            key = event.get('call_id')
            if not isinstance(key, str):
                return
            if key in self.pending:
                self.pending.pop(key).finish()
            name = event.get('name')
            name = redact_text(name)[:256] if isinstance(name, str) else 'unknown'
            span = Span(self.tracer, name, 'TOOL', parent=self,
                        attributes={**self.base_attributes(), 'tool.name': name})
            arguments = event.get('input', event.get('arguments'))
            if arguments is not None:
                span.payload('input', arguments)
                span.data['attributes']['popot.input.available'] = 'input' in event
                for suffix in ('truncated', 'bytes', 'sha256'):
                    if 'input_' + suffix in event:
                        span.data['attributes']['popot.input.' + suffix] = event['input_' + suffix]
            path = arguments.get('path', '') if isinstance(arguments, dict) else ''
            if event.get('name') == 'read_file' and isinstance(path, str):
                for skill in self.data['attributes']['popot.skills.configured']:
                    if '/skills/' + skill + '/' in '/' + path:
                        span.data['attributes']['popot.skill.reference_requested'] = skill
            self.pending[key] = span
        elif kind in {'tool_result', 'tool_error'}:
            key = event.get('call_id')
            if not isinstance(key, str):
                return
            span = self.pending.get(key)
            if span is None and kind == 'tool_error':
                self.event(json.dumps({**event, 'event': 'tool_call'}))
                span = self.pending.get(key)
                span.data['attributes']['popot.call_started_missing'] = True
            if span:
                if 'output' in event:
                    span.payload('output', event['output'])
                elif kind == 'tool_error':
                    span.payload('output', {k: event[k] for k in ('error_type', 'message') if k in event})
                span.data['attributes']['popot.output.available'] = 'output' in event
                for suffix in ('truncated', 'bytes', 'sha256'):
                    if 'output_' + suffix in event:
                        span.data['attributes']['popot.output.' + suffix] = event['output_' + suffix]
                if kind == 'tool_error':
                    span.data['status_code'] = 'ERROR'
                    if isinstance(event.get('error_type'), str):
                        span.data['attributes']['popot.error_type'] = redact_text(event['error_type'])[:160]
                elif span.data['status_code'] != 'ERROR' and 'popot.skill.reference_requested' in span.data['attributes']:
                    span.data['attributes']['popot.skill.reference_read'] = span.data['attributes']['popot.skill.reference_requested']
                if kind == 'tool_result':
                    self.pending.pop(key)
                    span.finish()

    def base_attributes(self):
        return {key: value for key, value in self.data['attributes'].items()
                if key in {'session.id', 'popot.role', 'popot.agent_profile', 'metadata',
                           'popot.agent_session_id', 'popot.root_session_id', 'popot.parent_session_id'}}

    def result(self, result):
        if not isinstance(result, dict):
            return
        outcome = result.get('outcome')
        outcome = outcome if isinstance(outcome, dict) else {}
        self.outcome = outcome.get('status', 'completed')
        self.payload('output', {key: result[key] for key in ('answer', 'outcome', 'revision') if key in result})
        if isinstance(outcome.get('reason'), str):
            self.data['attributes']['popot.outcome_reason'] = redact_text(outcome['reason'])[:256]

    def finish(self):
        for span in self.pending.values():
            span.data['status_code'] = 'ERROR'
            span.data['attributes']['popot.incomplete'] = True
            span.finish()
        self.pending.clear()
        self.data['attributes']['popot.outcome'] = self.outcome or 'interrupted'
        if self.outcome in {None, 'failed', 'blocked'}:
            self.data['status_code'] = 'ERROR'
        super().finish()


class PhoenixTracer:
    def __init__(self, settings, *, api_key):
        self.settings, self.api_key = dict(settings), api_key
        self.outbox = Path(settings['outbox_directory'])
        self.lock = threading.RLock()
        self.active = {}
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._run, name='phoenix-export', daemon=True)
        self.thread.start()

    @contextmanager
    def session(self, session_id, role, config, *, parent_session_id=None):
        with self.lock:
            parent = self.active.get(parent_session_id)
            span = AgentSpan(self, session_id, role or 'unassigned', config or {}, parent)
            self.active[session_id] = span
        try:
            yield span
        finally:
            with self.lock:
                if self.active.get(session_id) is span:
                    del self.active[session_id]
            span.finish()

    def model_span(self, session_id, model):
        with self.lock:
            parent = self.active.get(session_id)
            attrs = parent.base_attributes() if parent else {'popot.attribution_missing': True}
            return Span(self, model, 'LLM', parent=parent,
                        attributes={**attrs, 'llm.model_name': model, 'popot.requested_model': model})

    def submit(self, span):
        try:
            self.outbox.mkdir(parents=True, mode=0o700, exist_ok=True)
            path = self.outbox / (span['context']['span_id'] + '.json')
            temporary = path.with_suffix('.tmp')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w') as output:
                json.dump({'project': self.settings['project'], 'span': span}, output)
            temporary.replace(path)
            self.wake.set()
        except OSError as exc:
            _warning(exc)

    def _deliver(self, record):
        span = record['span']
        endpoint = self.settings['endpoint'].rstrip('/') + '/v1/projects/' + parse.quote(record['project'], safe='') + '/spans'
        req = request.Request(endpoint, data=json.dumps({'data': [span]}).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.api_key})
        try:
            with request.urlopen(req, timeout=self.settings['request_timeout_seconds']) as response:
                result = json.loads(response.read(65536))
                if not isinstance(result, dict) or result.get('total_received') != 1 or result.get('total_queued') != 1:
                    raise ValueError('Phoenix did not accept the span')
        except error.HTTPError as exc:
            with exc:
                body = exc.read(65536)
            if exc.code == 400:
                response = json.loads(body)
                detail = response.get('detail', {}) if isinstance(response, dict) else {}
                detail = json.loads(detail) if isinstance(detail, str) else detail
                duplicates = detail.get('duplicate_spans', []) if isinstance(detail, dict) else []
                if (isinstance(detail, dict) and detail.get('total_received') == 1 and detail.get('total_duplicates') == 1
                        and detail.get('total_invalid') == 0 and len(duplicates) == 1
                        and duplicates[0].get('span_id') == span['context']['span_id']):
                    return
            raise

    def _run(self):
        while not self.stop.is_set():
            failed = False
            for path in sorted(self.outbox.glob('*.json')):
                if self.stop.is_set():
                    break
                try:
                    self._deliver(json.loads(path.read_text()))
                    path.unlink()
                except (OSError, ValueError, KeyError) as exc:
                    _warning(exc)
                    failed = True
                    break
            if failed:
                self.stop.wait(self.settings['retry_seconds'])
            else:
                self.wake.wait(self.settings['retry_seconds'])
                self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        self.thread.join(self.settings['request_timeout_seconds'] + 1)
