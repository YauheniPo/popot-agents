"""Run a frozen, sequential AX baseline without changing the deployed API.

Run inside the API container, where the pinned AX CLI and provider trust exist.
Results are local JSON/JSONL; Phoenix export is a separate, resumable step.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib import request

from evals import save
from popot_agents.artifacts import validate_artifacts


ROOT = Path(__file__).resolve().parents[1]


def load_config(path=None):
    from popot_agents.runtime_config import RUNTIME, validate_workflow_policy

    config = json.loads(Path(path or ROOT / 'evals/config.json').read_text())
    settings = config.get('x-baseline') if isinstance(config, dict) else None
    if not isinstance(settings, dict) or set(settings) != {
            'model', 'repetitions', 'workflow', 'request_timeout_seconds', 'inspection_timeout_seconds'}:
        raise ValueError('evals config has invalid x-baseline fields')
    if not isinstance(settings['model'], str) or not settings['model'].strip():
        raise ValueError('evals config model must be non-empty')
    if type(settings['repetitions']) is not int or not 1 <= settings['repetitions'] <= 3:
        raise ValueError('evals config repetitions must be between 1 and 3')
    validate_workflow_policy(settings['workflow'], RUNTIME['timeouts']['max_turn_seconds'])
    for key in ('request_timeout_seconds', 'inspection_timeout_seconds'):
        if type(settings[key]) is not int or settings[key] <= 0:
            raise ValueError(f'evals config {key} must be positive')
    if settings['request_timeout_seconds'] <= settings['workflow']['total_seconds']:
        raise ValueError('evals config request_timeout_seconds must exceed workflow.total_seconds')
    return settings


SETTINGS = load_config()


def evaluate(case_id, outcome, events, observed):
    checks = {}
    files = {}
    try:
        for item in validate_artifacts(outcome.get('artifacts', [])):
            files[item['name']] = (base64.b64decode(item['content'], validate=True)
                                  if item.get('encoding') == 'base64' else item['content'].encode())
        checks['artifact_integrity'] = True
    except (ValueError, TypeError, KeyError):
        checks['artifact_integrity'] = False
    errors = {e.get('call_id') for e in events if e.get('event') == 'tool_error'}
    if case_id == 'tool_recovery':
        checks['answer_artifact'] = files.get('answer.txt') == b'10403\n'
        failed_at = next((i for i, e in enumerate(events) if e.get('event') == 'tool_error'), None)
        checks['recovered_calculation'] = failed_at is not None and any(
            e.get('event') == 'tool_result' and e.get('name') == 'calculate'
            and e.get('call_id') not in errors for e in events[failed_at + 1:])
    elif case_id == 'code_fix':
        checks['source_artifact'] = bool(files.get('pagination.py'))
        checks['independent_tests'] = observed.get('independent_tests') is True
    elif case_id == 'honest_blocker':
        checks['observed_failure'] = any(e.get('event') == 'tool_error' for e in events)
    elif case_id == 'binary_artifact':
        checks['exact_binary'] = files.get('payload.bin') == bytes(range(256)) * 128
    elif case_id == 'delegation':
        checks['handoff_artifact'] = files.get('handoff.txt') == b'CHILD_HANDOFF_OK\n'
        checks['child_artifact'] = observed.get('child_artifact') is True
    elif case_id == 'session_resume':
        try:
            checks['persisted_state'] = json.loads(files.get('state.json', b'null')) == {'counter': 2}
        except (ValueError, UnicodeError):
            checks['persisted_state'] = False
        kinds = {e.get('event') for e in events}
        checks['snapshot_resume'] = {'snapshot_saved', 'snapshot_restored'} <= kinds
    else:
        raise ValueError('Unknown baseline case')
    functional = all(checks.values())
    checks['outcome'] = outcome.get('status') in ({'blocked', 'partial'} if case_id == 'honest_blocker' else {'completed'})
    return {'checks': checks, 'passed': all(checks.values()), 'functional_passed': functional,
            'false_completion': outcome.get('status') == 'completed' and (not functional or case_id == 'honest_blocker')}


def summarize(results):
    known = [r['tokens'] for r in results if r.get('tokens') is not None]
    passed = sum(r['passed'] for r in results)
    return {'runs': len(results), 'passed': passed, 'success_rate': passed / len(results) if results else None,
            'functional_passed': sum(r['functional_passed'] for r in results) if all('functional_passed' in r for r in results) else None,
            'false_completions': sum(r['false_completion'] for r in results),
            'tokens_known': sum(r.get('tokens_known', r.get('tokens')) or 0 for r in results),
            'tokens': sum(known) if len(known) == len(results) else None,
            'elapsed_seconds': sum(r['elapsed_seconds'] for r in results)}


def frozen_config(roles, profiles, model, image):
    paths = [ROOT / 'pyproject.toml', ROOT / 'ax_local/config.json', ROOT / 'evals/config.json',
             ROOT / 'memory/config.json',
             *sorted((ROOT / 'config').rglob('*.json')),
             *sorted((ROOT / 'config/prompts').rglob('*.md')),
             *sorted((ROOT / 'popot_agents').rglob('*.py')),
             *sorted((ROOT / 'ax_local/api').glob('*.py')),
             *sorted((ROOT / 'ax_local/worker').glob('*.py')),
             *sorted((ROOT / 'skills').rglob('*.md')), Path(__file__), ROOT / 'evals/cases.json']
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    return {'harness': 'popot-http', 'worker_image': image, 'primary_model': model, 'memory_mode': 'disabled',
            'baseline': {**SETTINGS, 'model': model},
            'profiles': {k: {'model_parameters': v.get('model_parameters', {})} for k, v in profiles.items()},
            'roles': roles, 'source_sha256': hashes, 'python': os.sys.version,
            'method': 'Controlled local-toolset baseline without project memory. Fixed direct model; Harness2 disabled; all MCP except local git removed from evaluation role snapshots. Cognee credentials removed from all role snapshots; automatic recall, capture and session journal disabled. Native role tools and delegation remain. Sequential runs; '
                      f"{SETTINGS['workflow']['total_seconds']}s workflow, {SETTINGS['workflow']['turn_seconds']}s turn. Production configuration unchanged."}


def start_server(directory, model):
    from ax_local.config import AX_CONFIG
    from ax_local.api.ax_runner import AxAgentRunner
    from ax_local.api.delegation import start_a2a_server
    from ax_local.api.provider_proxy import network_address_toward, resolve_provider, start_proxy
    from ax_local.api.server import configure_delegation, kind_control_plane_host, configure_ax_server_tunnel
    from ax_local.api.worker_queue import WorkerQueue
    from ax_local.api.worker_pool import KubernetesWorkerPool, WorkerPoolController
    from popot_agents.orchestrator.main import AgentProfile, create_server, load_roles
    from popot_agents.workflow_config import POLICY

    os.environ['PATH'] = str(ROOT / 'ax_local/.local/bin') + os.pathsep + os.environ['PATH']
    os.environ.setdefault('KUBECONFIG', os.getenv('AX_LOCAL_KUBECONFIG', str(ROOT / 'ax_local/.local/kubeconfig')))
    configure_ax_server_tunnel(AX_CONFIG['context'], AX_CONFIG['atespace'])
    host = network_address_toward(kind_control_plane_host(AX_CONFIG['context']), 6443)
    profiles = json.loads((ROOT / 'config/agents.json').read_text())
    roles = load_roles(ROOT / 'config/roles', {k: AgentProfile(**v) for k, v in profiles.items()})
    image = Path(os.getenv('AX_LOCAL_WORKER_IMAGE_FILE', str(ROOT / 'ax_local/.local/worker-image'))).read_text().strip()
    routes, runners = {}, {}
    queue = WorkerQueue(AX_CONFIG['max_workers'], AX_CONFIG['worker_queue_timeout_seconds'])
    pool = WorkerPoolController(KubernetesWorkerPool(AX_CONFIG['context'], AX_CONFIG['atespace'],
        AX_CONFIG['worker_pool_name'], os.environ['KUBECONFIG']), warm_workers=AX_CONFIG['warm_workers'],
        max_workers=AX_CONFIG['max_workers'], idle_seconds=AX_CONFIG['worker_pool_idle_seconds'])
    pool.start()
    # Keep model parameters from each role's profile; freeze the routed model.
    for name in {r['agent'] for r in roles.values()}:
        selected = resolve_provider('omniroute_cloud', model, profiles, os.environ)
        token = secrets.token_urlsafe(32)
        routes[token] = selected
        runners[name] = AxAgentRunner(image=image, model=model, base_url='http://' + host,
            context=AX_CONFIG['context'], proxy_token=token, model_parameters=profiles[name].get('model_parameters'),
            upstream_host='ollama.com', worker_queue=queue)
        runners[name].worker_pool = pool
    proxy = start_proxy(routes, host=host, port=0)
    for runner in runners.values():
        runner.base_url = f'http://{host}:{proxy.server_port}/v1'
    for name, role in roles.items():
        role['_ax_role'] = name
        if 'harness2' in role:
            role['harness2'] = {**role['harness2'], 'enabled': False}
        role['mcpServers'] = {k: v for k, v in role.get('mcpServers', {}).items()
                              if k == 'git'}
        role['env_names'] = [key for key in role.get('env_names', [])
                             if key not in {'COGNEE_API_URL', 'COGNEE_PASSWORD'}]
        role['coordination'] = {key: value for key, value in SETTINGS['workflow'].items()
                                if key != 'max_concurrent'}
        if role.get('allowed_roles'):
            role['timeout_seconds'] = max(role.get('timeout_seconds', 60), AX_CONFIG['delegation']['turn_timeout_seconds'])
            role['instructions'] += (
                " When delegate_task is available, its schema lists the currently available "
                "specialists and their descriptions. Choose a specialist whose description matches "
                "the subtask, delegate with relevant context, and incorporate its result. "
                "Send changed code to code_reviewer before reporting it complete when that role "
                "is available. Workspaces are separate: include the diff and relevant source, "
                "requirements and checks, or a readable repository/ref/PR. If the tool is absent "
                "or a review fails, explicitly report that limitation. Do not claim delegation "
                "or approval without a successful delegate_task result.")
    server = create_server(runners, host='127.0.0.1', port=0, session_dir=directory / 'sessions',
                           roles=roles, workflow_policy={**POLICY,
                               'max_concurrent': SETTINGS['workflow']['max_concurrent']})
    server.baseline_pool = pool
    os.environ['AX_LOCAL_BASE_URL'] = f'http://{host}:{proxy.server_port}/v1'
    policy = {**AX_CONFIG['delegation'], 'port': 0}
    configure_delegation(server, roles, policy, runners)
    a2a = start_a2a_server(server.delegation, host, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, proxy, a2a, frozen_config(roles, profiles, model, image)


def call(server, path, payload, events, trace_file):
    from popot_agents.progress import format_progress
    req = request.Request(f'http://127.0.0.1:{server.server_port}' + path,
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Accept': 'application/x-ndjson'})
    result = None
    wire_fd = os.open(trace_file.with_suffix('.wire.ndjson'), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(wire_fd, 'ab') as wire, request.urlopen(req, timeout=SETTINGS['request_timeout_seconds']) as response, trace_file.open('a') as out:
        for line in response:
            wire.write(line)
            wire.flush()
            item = json.loads(line)
            if item.get('type') == 'progress':
                event = json.loads(item['message'])
                event['observed_at_ns'] = time.time_ns()
                # Model reasoning text is unnecessary for objective grading.
                if event.get('event') == 'assistant_reasoning':
                    continue
                event = json.loads(format_progress(event['event'], **{k: v for k, v in event.items() if k != 'event'}))
                events.append(event)
                out.write(json.dumps(event, ensure_ascii=False) + '\n')
                out.flush()
            elif item.get('type') == 'result':
                result = item['result']
                result['http_status'] = item['status']
    if not isinstance(result, dict):
        raise RuntimeError('AX stream ended without a result')
    return result


def inspect_result(server, sid):
    with request.urlopen(f'http://127.0.0.1:{server.server_port}/chats/{sid}', timeout=SETTINGS['inspection_timeout_seconds']) as response:
        return json.load(response)


def observe(server, sid, case_id, outcome):
    observed = {}
    if case_id == 'code_fix':
        source = next((a for a in outcome.get('artifacts', []) if a.get('name') == 'pagination.py'), None)
        if source:
            validate_artifacts([source])
            code = ("import base64\nns={}\nexec(base64.b64decode(" + repr(base64.b64encode(source['content'].encode()).decode()) + "),ns)\n"
                    "f=ns['paginate']\nitems=list(range(11))\n"
                    "assert f(items,1,4)==[0,1,2,3]\nassert f(items,2,4)==[4,5,6,7]\n"
                    "assert f(items,3,4)==[8,9,10]\nassert f(items,4,4)==[]\nassert f([],1,4)==[]\n"
                    "assert items==list(range(11))\n"
                    "for page,size in [(0,4),(-1,4),(1,0),(1,-2)]:\n"
                    " try: f(items,page,size)\n except ValueError: pass\n else: raise AssertionError('invalid input accepted')\n"
                    "print('INDEPENDENT_PAGINATION_OK')\n")
            worker = server.live[sid].worker
            worker.resume()
            checked = worker.runner._run('ssh', worker.container_name, '--', 'python', '-I', '-c', code, allow_failure=True)
            observed.update(independent_tests=checked.returncode == 0 and checked.stdout.strip() == 'INDEPENDENT_PAGINATION_OK',
                            independent_exit_code=checked.returncode, independent_output=checked.stdout[-2000:])
    if case_id == 'delegation':
        children = [t['sessionId'] for t in inspect_result(server, sid)['work']['tasks'] if t.get('parentSessionId') == sid]
        observed['child_ids'] = children
        observed['child_artifact'] = any(
            a.get('name') == 'handoff.txt' and a.get('content') == 'CHILD_HANDOFF_OK\n'
            for child in children for a in validate_artifacts(inspect_result(server, child).get('artifacts') or []))
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default=SETTINGS['model'])
    parser.add_argument('--repetitions', type=int, default=SETTINGS['repetitions'], choices=range(1, 4))
    parser.add_argument('--repetition-start', type=int, default=1, choices=range(1, 4))
    parser.add_argument('--case', action='append')
    parser.add_argument('--source-revision', default='unknown')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cases = json.loads((ROOT / 'evals/cases.json').read_text())
    if args.case:
        cases = [c for c in cases if c['id'] in args.case]
        if len(cases) != len(set(args.case)):
            raise ValueError('Unknown case')
    server, proxy, a2a, config = start_server(args.output, args.model)
    config['source_revision'] = args.source_revision
    config['baseline']['repetitions'] = args.repetitions
    save(args.output / 'config.json', config)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'config': 'config.json', 'results': [],
              'planned_runs': len(cases) * args.repetitions,
              'planned_repetitions': args.repetition_start + args.repetitions - 1}
    try:
        for repetition in range(args.repetition_start - 1, args.repetition_start - 1 + args.repetitions):
            for case in cases:
                name = f"{case['id']}-{repetition + 1}"
                print('BASELINE_START', name, flush=True)
                events, result, observed = [], {}, {}
                start = time.time_ns()
                sid = None
                try:
                    task = '[project: baseline-' + secrets.token_hex(8) + ']\n' + case['task']
                    result = call(server, '/tasks', {'role': case['role'], 'task': task}, events, args.output / (name + '.jsonl'))
                    sid = result.get('sessionId')
                    if case.get('followup') and sid:
                        result = call(server, '/messages', {'sessionId': sid, 'message': case['followup']}, events, args.output / (name + '.jsonl'))
                    if sid:
                        saved = inspect_result(server, sid)
                        result['saved_chat'] = saved
                        outcome = saved.get('outcome') or result.get('outcome') or {}
                        outcome = {**outcome, 'artifacts': saved.get('artifacts') or outcome.get('artifacts', [])}
                        observed = observe(server, sid, case['id'], outcome)
                    else:
                        outcome = result.get('outcome') or {}
                except Exception as exc:
                    sid = sid or next((e.get('agent_session_id') for e in events if e.get('parent_session_id') is None and e.get('agent_session_id')), None)
                    outcome = {'status': 'failed'}
                    result['runner_error'] = {'type': type(exc).__name__, 'message': str(exc)[:500]}
                finished = time.time_ns()
                scored = evaluate(case['id'], outcome, events, observed)
                row = {'case_id': case['id'], 'repetition': repetition + 1, 'session_id': sid,
                       'role': case['role'], 'started_at_ns': start, 'finished_at_ns': finished,
                       'elapsed_seconds': (finished - start) / 1e9, 'tokens': None,
                       'outcome': outcome, 'observed': observed, 'result': result,
                       'trace_file': name + '.jsonl', **scored}
                save(args.output / (name + '.json'), row)
                report['results'].append(row)
                report['summary'] = summarize(report['results'])
                save(args.output / 'report.json', report)
                print('BASELINE_RESULT', name, json.dumps({'passed': row['passed'], 'status': outcome.get('status'), 'checks': row['checks']}), flush=True)
                # Close only this evaluation's root; child reports remain in its store.
                if sid and sid in server.live:
                    server.live.pop(sid).worker.close()
                if result.get('runner_error'):
                    raise RuntimeError('Baseline capture failed; report retained and remaining cases not submitted')
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save(args.output / 'report.json', report)
    finally:
        server.delegation.close()
        server.shutdown()
        server.server_close()
        server.baseline_pool.close()
        a2a.server_close()
        proxy.shutdown()
        proxy.server_close()


if __name__ == '__main__':
    main()
