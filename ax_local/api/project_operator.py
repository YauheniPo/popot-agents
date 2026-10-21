"""Local project diagnostics and reviewable changes, behind an actor capability."""

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from urllib import request
from urllib.parse import quote, urlencode
import uuid

from ax_local.config import AX_CONFIG
from ax_local.cluster.holmes import investigation_job, manifests, scope
from ax_local.omniroute import Api, NoRedirect, contains
from ax_local.resource_config import SERVICES, load_monitoring, validate_resources
from popot_agents.progress import redact_text, trace_event

ROOT = Path(__file__).resolve().parents[2]


def safe_result(value):
    sensitive = {'apikey', 'apikeys', 'key', 'keyhash', 'password', 'secret', 'authorization',
                 'cookie', 'token', 'accesstoken', 'refreshtoken', 'providerspecificdata',
                 'byapikey', 'byaccount'}
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.sub('[^a-z]', '', k.lower()) in sensitive else safe_result(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [safe_result(v) for v in value]
    return redact_text(value) if isinstance(value, str) else value


class ProjectOperations:
    def __init__(self, roles, store, *, root=ROOT):
        self.roles, self.store, self.root = roles, store, Path(root)
        self.plans = {}
        self.lock = threading.RLock()
        self.catalog = {t['model']: t for c in self._combos().values() for t in c['models']}
        self.namespaces = scope(AX_CONFIG)

    def _combos(self):
        return json.loads((self.root / 'config/omniroute/combos.json').read_text())

    def _save(self, name, value):
        path = self.root / 'config' / name
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(path)

    def _kube(self, *arguments, payload=None):
        if not AX_CONFIG['context'].startswith('kind-'):
            raise ValueError('operations require the local kind cluster')
        result = subprocess.run(['kubectl', '--context=' + AX_CONFIG['context'],
            '--request-timeout=10s', *arguments], input=json.dumps(payload) if payload is not None else None,
            text=True, capture_output=True, timeout=12, check=False)
        if result.returncode:
            # kubectl errors can echo submitted fields; never return its body.
            raise RuntimeError('Local Kubernetes request failed; inspect service health')
        if len(result.stdout.encode()) > 512 * 1024:
            raise ValueError('Kubernetes response too large; narrow the request')
        return result.stdout

    def _json(self, url):
        opener = request.build_opener(NoRedirect(), request.ProxyHandler({}))
        with opener.open(url, timeout=8) as response:
            data = response.read(512 * 1024 + 1)
        if len(data) > 512 * 1024:
            raise ValueError('Service response too large; narrow the request')
        return json.loads(data)

    def _proxy(self, service, path):
        ports = {'grafana': 3000, 'prometheus': 9090}
        return json.loads(self._kube('get', '--raw',
            f'/api/v1/namespaces/otel-system/services/{service}:{ports[service]}/proxy' + path))

    def _http_status(self, url):
        client = request.build_opener(NoRedirect(), request.ProxyHandler({}))
        with client.open(url, timeout=5) as response:
            return {'http_status':response.status, 'content_type':response.headers.get('Content-Type')}

    def _mcp_status(self):
        with socket.create_connection(('mcp-server',8001),timeout=3):
            return {'tcp_reachable':True, 'note':'TCP reachability only; not a tool execution check'}

    def _api_health(self):
        from .provider_proxy import network_address_toward
        host = os.getenv('AX_LOCAL_BIND_HOST') or network_address_toward('db',5432)
        return self._json('http://' + host + ':' + os.getenv('AX_LOCAL_API_PORT','8002') + '/healthz')

    def _gateway(self):
        api = Api('http://omniroute:20128', timeout=8)
        api.request('POST', '/api/auth/login', {'password': os.environ['OMNIROUTE_INITIAL_PASSWORD']})
        return api

    def _memory(self):
        from popot_agents.worker.cognee_memory import CogneeMemory
        return CogneeMemory(os.environ['COGNEE_API_URL'], os.environ['COGNEE_PASSWORD'], timeout=8)

    def read_combo(self, name):
        rows = self._gateway().request('GET', '/api/combos')['combos']
        found = next((r for r in rows if r['name'] == name), None)
        if found is None:
            raise ValueError('managed combo not found')
        return {k: found[k] for k in ('name', 'description', 'strategy', 'models', 'config') if k in found}

    def write_combo(self, name, combo):
        api = self._gateway()
        rows = api.request('GET', '/api/combos')['combos']
        row = next(r for r in rows if r['name'] == name)
        api.request('PUT', '/api/combos/' + quote(row['id'], safe=''), combo)
        current = self.read_combo(name)
        if any(not contains(current.get(k),combo[k]) for k in ('models', 'strategy', 'config')):
            raise RuntimeError('Combo write could not be verified')

    def _deployment(self, name):
        if name not in SERVICES:
            raise ValueError('only local monitoring deployments can be changed')
        return json.loads(self._kube('-n', 'otel-system', 'get', 'deployment', name, '-o', 'json'))

    def inspect(self, kind, params, *, actor='', request_id=''):
        if not isinstance(params, dict):
            raise ValueError('parameters must be an object')
        if kind == 'overview':
            result = {}
            for name, function in {
                'ax_api': self._api_health,
                'mcp_gateway': self._mcp_status,
                'omniroute': lambda: self._gateway().request('GET','/api/monitoring/health'),
                'cognee': lambda: self._json('http://cognee:8000/health'),
                'cognee_ui': lambda: self._http_status('http://host.docker.internal:3001/sessions'),
                'grafana': lambda: self._proxy('grafana', '/api/health'),
                'prometheus': lambda: self._proxy('prometheus', '/api/v1/query?' + urlencode({'query':'up'})),
                'postgres': self._database,
            }.items():
                try:
                    result[name] = safe_result(function())
                except Exception as exc:
                    result[name] = {'unavailable': True, 'error_type': type(exc).__name__}
            result['roles'] = {n: {k: v for k, v in r.items() if k in
                {'agent', 'harness2', 'timeout_seconds', 'max_tool_rounds'}} for n, r in self.roles.items()}
            return result
        if kind in {'pods', 'events', 'logs'}:
            namespace = params.get('namespace', AX_CONFIG['atespace'])
            if namespace not in self.namespaces:
                raise ValueError('namespace is outside the local project')
            if kind == 'logs':
                pod, container = params.get('pod', ''), params.get('container', '')
                if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', pod) or not re.fullmatch(
                        r'[a-z0-9][a-z0-9-]{0,62}', container):
                    raise ValueError('valid pod and container names are required')
                args = ['-n',namespace,'logs',pod,'-c',container,'--tail=200','--since=30m','--timestamps=true']
                if params.get('previous') is True:
                    args.append('--previous')
                return {'logs': redact_text(self._kube(*args))}
            rows = json.loads(self._kube('-n',namespace,'get',kind,'-o','json'))['items']
            if kind == 'events':
                return safe_result([{'time': r.get('lastTimestamp'), 'type': r.get('type'),
                    'reason': r.get('reason'), 'object': r.get('involvedObject', {}).get('name'),
                    'message': r.get('message')} for r in rows[-100:]])
            return [{'namespace': namespace, 'pod': r['metadata']['name'], 'status': r.get('status'),
                'resources': [{'container': c['name'], 'resources': c.get('resources', {})}
                              for c in r['spec']['containers']]} for r in rows]
        if kind == 'metrics':
            query = params.get('query', '')
            if not isinstance(query, str) or not query.strip() or len(query) > 3000:
                raise ValueError('PromQL query of 1..3000 characters is required')
            args = {'query': query, 'timeout':'5s'}
            if 'start' in params or 'end' in params:
                start, end, step = params.get('start'), params.get('end'), params.get('step', 60)
                if any(type(v) not in {int, float} for v in (start, end, step)) \
                        or not 0 < end - start <= 7 * 86400 or step < 15 or (end-start)/step > 1500:
                    raise ValueError('range must be <=7d, step>=15s and <=1500 points')
                args.update(start=start, end=end, step=step)
            endpoint = 'query_range' if 'start' in args else 'query'
            return self._proxy('prometheus', '/api/v1/' + endpoint + '?' + urlencode(args))
        if kind == 'models':
            api = self._gateway()
            return safe_result({'combos': api.request('GET','/api/combos'),
                'health': api.request('GET','/api/monitoring/health'),
                'usage': api.request('GET','/api/usage/history'),
                'combo_metrics': api.request('GET','/api/combos/metrics')})
        if kind == 'memory':
            period = params.get('range', '24h')
            if period not in {'24h','7d','30d','all'}:
                raise ValueError('invalid memory time range')
            client = self._memory()
            return safe_result({'stats':client._request('/api/v1/sessions/stats?range=' + period),
                'sessions':client._request('/api/v1/sessions?limit=20&range=' + period),
                'datasets':client._request('/api/v1/datasets')})
        if kind == 'sessions':
            rows = self.store.list_chats() if self.store else []
            return {'total':len(rows),'recent':[{k:v for k,v in r.items() if k in
                {'sessionId','agent','role','createdAt','updatedAt'}} for r in rows[:30]],
                'postgres':self._database()}
        if kind == 'holmes':
            return self._holmes(params, actor, request_id)
        raise ValueError('unknown inspection; use overview, pods, events, logs, metrics, models, memory, sessions or holmes')

    def _database(self):
        if self.store is None or not hasattr(self.store, '_connect'):
            return {'backend':'file'}
        with self.store._connect() as connection:
            row = connection.execute('SELECT pg_database_size(current_database()), '
                '(SELECT count(*) FROM pg_stat_activity WHERE datname=current_database())').fetchone()
        return {'database_bytes':row[0], 'connections':row[1]}

    def _holmes(self, params, actor, request_id):
        job = params.get('job')
        if job is None:
            question = params.get('question', '')
            if not isinstance(question,str) or not 1 <= len(question.strip()) <= 6000 or not actor or not request_id:
                raise ValueError('Holmes needs a question and an active operator request')
            with self.lock:
                label = hashlib.sha256((actor + request_id).encode()).hexdigest()[:24]
                existing = json.loads(self._kube('-n','ax-diagnostics','get','jobs',
                    '-l','popot.dev/operator-request=' + label,'-o','json'))['items']
                if existing:
                    job = existing[0]['metadata']['name']
                else:
                    active = json.loads(self._kube('-n','ax-diagnostics','get','jobs',
                        '-l','popot.dev/operator-session=' + actor,'-o','json'))['items']
                    if sum(not r.get('status',{}).get('completionTime') and not r.get('status',{}).get('failed') for r in active) >= 2:
                        raise ValueError('two Holmes investigations are already active; inspect them first')
                    self._kube('apply','--server-side','-f','-',payload={'apiVersion':'v1','kind':'List',
                        'items':manifests(AX_CONFIG)})
                    self._kube('apply','--server-side','-f','-',payload={'apiVersion':'v1','kind':'Secret',
                        'metadata':{'name':'holmes-omniroute','namespace':'ax-diagnostics'},'type':'Opaque',
                        'stringData':{'api-key':os.environ['OMNIROUTE_API_KEY']}})
                    spec = investigation_job(question, os.environ['HOLMES_MODEL'])
                    spec['metadata']['labels'] = {'popot.dev/operator-session': actor,
                                                  'popot.dev/operator-request': label}
                    job = json.loads(self._kube('create','-f','-','-o','json',payload=spec))['metadata']['name']
        if not isinstance(job,str) or not re.fullmatch(r'holmes-ax-[a-z0-9-]{1,50}',job):
            raise ValueError('invalid Holmes job name')
        saved = json.loads(self._kube('-n','ax-diagnostics','get','job',job,'-o','json'))
        wait = params.get('wait_seconds', 5)
        if type(wait) not in {int,float} or not 0 <= wait <= 15:
            raise ValueError('Holmes wait_seconds must be 0..15')
        if not saved.get('status',{}).get('succeeded') and not saved.get('status',{}).get('failed') and wait:
            time.sleep(wait)
            saved = json.loads(self._kube('-n','ax-diagnostics','get','job',job,'-o','json'))
        result = {'job':job, 'status':saved.get('status',{}), 'model':os.environ['HOLMES_MODEL']}
        try:
            result['report'] = redact_text(self._kube('-n','ax-diagnostics','logs','job/'+job,
                                                    '--tail=200','--timestamps=true'))
        except (RuntimeError, subprocess.TimeoutExpired):
            result['report'] = 'Holmes pod is not ready yet. Poll this job in a later tool call.'
        return result

    def propose(self, actor, change):
        with self.lock:
            self.plans = {k:v for k,v in self.plans.items() if time.monotonic() < v['expires']}
            if sum(p['actor'] == actor for p in self.plans.values()) >= 10:
                raise ValueError('too many plans in this session')
            change = copy.deepcopy(change)
            action = change.get('action')
            if action == 'configure_combo':
                name = change.get('name')
                source = self._combos()
                if name not in source:
                    raise ValueError('only managed combos may be changed')
                models = change.get('models')
                if not isinstance(models,list) or not models \
                        or any(not isinstance(m,str) or m not in self.catalog for m in models) \
                        or len(models) != len(set(models)):
                    raise ValueError('use a nonempty list of approved models from the managed config')
                before = self.read_combo(name)
                after = copy.deepcopy(before)
                after['models'] = [copy.deepcopy(self.catalog[m]) for m in models]
                after['strategy'] = 'priority'
                # Preserve actual connection IDs without exposing credentials.
                connections = {t['providerId']: t.get('connectionId') for c in source.values()
                               for t in c['models'] if t.get('connectionId')}
                connections.update({t['providerId']:t['connectionId'] for t in before['models'] if t.get('connectionId')})
                if any(t['providerId'] not in connections for t in after['models']):
                    # Other managed combos already carry the provisioned account IDs.
                    # Never inspect provider credentials to resolve a model target.
                    rows = self._gateway().request('GET','/api/combos')['combos']
                    connections.update({t['providerId']:t['connectionId'] for c in rows if c['name'] in source
                                        for t in c['models'] if t.get('connectionId')})
                for target in after['models']:
                    if connections.get(target['providerId']):
                        target['connectionId'] = connections[target['providerId']]
                baseline = source
            elif action in {'set_resources','restart_service'}:
                name = change.get('service')
                current = self._deployment(name)
                before = current['spec']['template']['spec']['containers'][0].get('resources',{})
                after = validate_resources(change.get('resources')) if action == 'set_resources' else {'restart':True}
                baseline = load_monitoring(self.root)
            else:
                raise ValueError('supported actions: configure_combo, set_resources, restart_service')
            plan_id = 'change-' + uuid.uuid4().hex[:24]
            result = {'plan_id':plan_id,'action':action,'target':name,'before':before,'after':after,
                      'apply_command':'/apply ' + plan_id, 'expires_in_seconds':900}
            self.plans[plan_id] = {**result,'actor':actor,'expires':time.monotonic()+900,
                                   'baseline':baseline,'result':None}
            return copy.deepcopy(result)

    def apply(self, actor, plan_id, *, approved, authorize=None):
        with self.lock:
            plan = self.plans.get(plan_id)
            if not plan or plan['expires'] <= time.monotonic():
                raise ValueError('plan is missing or expired; request a new plan')
            if plan['actor'] != actor:
                raise ValueError('plan belongs to another session')
            if approved != plan_id:
                raise ValueError('explicit user approval required: send ' + plan['apply_command'])
            if plan['result'] is not None:
                return copy.deepcopy(plan['result'])
            name, action = plan['target'], plan['action']
            if action == 'configure_combo':
                if self.read_combo(name) != plan['before'] or self._combos() != plan['baseline']:
                    raise ValueError('configuration changed; request a new plan')
            else:
                current = self._deployment(name)
                if current['spec']['template']['spec']['containers'][0].get('resources',{}) != plan['before'] \
                        or load_monitoring(self.root) != plan['baseline']:
                    raise ValueError('configuration changed; request a new plan')
            if authorize:
                grant = authorize()
                if grant.approved_change != plan_id:
                    raise ValueError('current turn no longer approves this plan')
            # Claim before side effects. Lost/failed responses must never repeat a mutation.
            plan['result'] = {'plan_id':plan_id,'status':'failed_or_unknown','isError':True,
                              'message':'Inspect current state before proposing another change.'}
            try:
                if action == 'configure_combo':
                    self.write_combo(name, plan['after'])
                    config = self._combos()
                    config[name] = copy.deepcopy(plan['after'])
                    for target in config[name]['models']:
                        target.pop('connectionId',None)
                    self._save('omniroute/combos.json',config)
                else:
                    path = '/spec/template/spec/containers/0/resources'
                    value = plan['after']
                    if action == 'restart_service':
                        path = '/spec/template/metadata/annotations'
                        value = {**current['spec']['template']['metadata'].get('annotations',{}),
                                 'popot.dev/restarted-at':datetime.now(timezone.utc).isoformat()}
                    self._kube('-n','otel-system','patch','deployment',name,'--type=json','-p',json.dumps([
                        {'op':'test','path':'/metadata/resourceVersion','value':current['metadata']['resourceVersion']},
                        {'op':'add','path':path,'value':value}]))
                    if action == 'set_resources':
                        config = load_monitoring(self.root)
                        config.setdefault(name, {})['resources'] = plan['after']
                        self._save('monitoring.json',config)
                    verified = self._deployment(name)
                    actual = (verified['spec']['template']['spec']['containers'][0].get('resources',{})
                              if action == 'set_resources' else verified['spec']['template']['metadata'].get('annotations',{}))
                    if actual != value:
                        raise RuntimeError('Kubernetes change could not be verified')
                plan['result'] = {'plan_id':plan_id,'status':'applied','target':name,'after':plan['after'],
                                  'note':'Configuration verified. Check service readiness separately.'}
            except Exception as exc:
                plan['result']['error_type'] = type(exc).__name__
            trace_event('project_change', session_id=actor, plan_id=plan_id,
                        action=action, status=plan['result']['status'])
            return copy.deepcopy(plan['result'])
