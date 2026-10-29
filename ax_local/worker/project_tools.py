"""Tools for the AX operator. Service credentials remain in the API."""

import json
import os
from urllib import error, request


def make_tools(environ=None):
    env = os.environ if environ is None else environ
    if env.get('AX_DELEGATION_OPERATOR') != '1':
        return {}
    endpoint = env['AX_DELEGATION_URL'].rstrip('/') + '/operations'
    token = env['AX_DELEGATION_TOKEN']
    specs = {
        'project_inspect': ('inspect', 'Inspect local project services. For Kubernetes investigations start Holmes with '
            'kind=holmes, parameters={question:...}; then poll with parameters={job:...}. '
            'Other kinds: overview, pods/events/logs (namespace, pod, container, previous), '
            'metrics (PromQL query; optional numeric start/end/step), models, memory (range), sessions.',
            {'kind':{'type':'string','enum':['overview','pods','events','logs','metrics','models','memory','sessions','holmes']},
             'parameters':{'type':'object'}}, ['kind']),
        'project_propose_change': ('propose_change', 'Prepare a precise change plan without applying it. '
            'Supported: configure_combo (name, models array of approved model IDs), set_resources '
            '(service, resources with cpu/memory requests/limits), restart_service (service). '
            'Return the complete plan and /apply command to the user.',
            {'action':{'type':'string','enum':['configure_combo','set_resources','restart_service']},
             'name':{'type':'string'},'models':{'type':'array','items':{'type':'string'}},
             'service':{'type':'string','enum':['grafana','prometheus','kube-state-metrics','opentelemetry-collector']},
             'resources':{'type':'object'}}, ['action']),
        'project_apply_change': ('apply_change', 'Apply a previously reviewed plan only when the current user '
            'message is exactly /apply <plan_id>. The server verifies approval; do not retry unknown effects.',
            {'plan_id':{'type':'string'}}, ['plan_id']),
    }
    tools = {}
    for name, (method, description, properties, required) in specs.items():
        def call(arguments, *, timeout_seconds, call_id, method=method):
            body = json.dumps({'method':method,'params':arguments,'request_id':call_id}).encode()
            req = request.Request(endpoint,data=body,headers={'Content-Type':'application/json',
                                  'Authorization':'Bearer '+token},method='POST')
            client = request.build_opener(request.ProxyHandler({}))
            try:
                with client.open(req,timeout=min(60,timeout_seconds)) as response:
                    raw = response.read(128000)
                result = json.loads(raw)
            except error.HTTPError as exc:
                try:
                    result = json.loads(exc.read(2000))
                    if not isinstance(result, dict):
                        result = {}
                    result.update(isError=True)
                    result.setdefault('error', 'Project operation rejected (HTTP ' + str(exc.code) + ')')
                finally:
                    exc.close()
            except (OSError, ValueError):
                result = {'isError':True,'error':'Operator request unavailable or timed out; inspect current state before retrying a change.'}
            return json.dumps(result,ensure_ascii=False)
        tools[name] = {'schema':{'type':'function','function':{'name':name,'description':description,
            'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}},
            'call':call}
    return tools
