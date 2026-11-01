"""Bounded autonomous coordinator turns, persisted with the existing session."""
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from popot_agents.runtime_config import RUNTIME


from popot_agents.workflow_config import POLICY, validate_policy, role_policy


def public_workflow(saved, *, include_result=False):
    meta = (saved.get('roleConfig') or {}).get('_workflow')
    if not meta:
        return None
    result = {key: meta.get(key) for key in ('state', 'phase', 'turns', 'deadlineAt')}
    result.update(runId=meta.get('run_id'), startedAt=meta.get('started_at'),
                  finishedAt=meta.get('finished_at'), waitingFor=meta.get('waiting_for'))
    result['deadlineAt'] = datetime.fromtimestamp(meta['deadlineAt'], timezone.utc).isoformat()
    if include_result and meta['state'] in {'completed', 'blocked', 'failed'}:
        messages = saved.get('messages', [])
        answer = messages[-1]['content'] if messages and messages[-1]['role'] == 'assistant' else ''
        result['result'] = {'answer': meta.get('blocked_message', answer), 'outcome': meta.get('outcome')}
    return result


class WorkflowRun:
    def __init__(self, policy, message, previous=None):
        self.policy = policy
        self.meta = dict(previous) if previous else {
            'state': 'running', 'phase': 'checkpoint', 'turns': 0,
            'deadlineAt': time.time() + policy['total_seconds'], 'original_task': message,
            'no_progress': 0,
        }
        self.meta.setdefault('run_id', uuid.uuid4().hex)
        self.meta.setdefault('started_at', datetime.now(timezone.utc).isoformat())
        self.meta.setdefault('authorization', [self.meta.get('original_task', message)])
        self.deadline = time.monotonic() + max(0, self.meta['deadlineAt'] - time.time())

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def begin(self):
        increment = 0 if self.meta.pop('awaiting_children',False) else 1
        self.meta.update(state='running', phase='executing', turns=self.meta['turns'] + increment)

    def record(self, result, checkpoint):
        outcome = result.get('outcome')
        if not isinstance(outcome, dict) or outcome.get('status') not in {'completed', 'partial', 'blocked', 'failed'}:
            return self.stop(result, 'workflow_missing_outcome')
        for key in ('evidence', 'remaining_work', 'errors'):
            if not isinstance(outcome.get(key), list) or any(not isinstance(x, str) for x in outcome[key]):
                return self.stop(result, 'workflow_invalid_outcome')
        status = outcome['status']
        if status == 'completed' and (not any(x.strip() for x in outcome['evidence'])
                or any(x.strip() for key in ('remaining_work', 'errors') for x in outcome[key])):
            return self.stop(result, 'workflow_unverified_completion')
        self.meta['outcome'] = outcome
        self.meta['outcomeRecordedAt'] = datetime.now(timezone.utc).isoformat()
        if status != 'partial':
            self.meta.update(state=status, phase='finished', finished_at=datetime.now(timezone.utc).isoformat())
            if status == 'completed':
                self.meta.pop('checkpoint', None)
            elif isinstance(checkpoint, dict):
                self.meta['checkpoint'] = checkpoint
            return result
        if not isinstance(checkpoint, dict):
            return self.stop(result, 'workflow_checkpoint_unavailable')
        self.meta['checkpoint'] = checkpoint
        if outcome.get('reason') == 'child_pending' and any(
                value.get('pending') for _,value in checkpoint.get('failed_delegations', [])):
            if self.remaining() < 1:
                return self.stop(result, 'workflow_deadline_exhausted')
            self.meta.update(state='running', phase='checkpoint', awaiting_children=True)
            return None
        material = {key: sorted(outcome[key]) for key in ('evidence', 'remaining_work', 'errors')}
        receipts = []
        for item in checkpoint.get('messages', []):
            if not isinstance(item, dict) or item.get('role') != 'tool':
                continue
            content = item.get('content','')
            try: value = json.loads(content)
            except (ValueError, TypeError): value = {}
            if isinstance(value, dict) and any(value.get(k) for k in ('isError','error','outcome_unknown','pending')):
                continue
            if isinstance(content, str) and content.startswith('exit_code=') and not content.startswith('exit_code=0\n'):
                continue
            receipts.append(hashlib.sha256((str(item.get('tool_call_id',''))+str(content)).encode()).hexdigest())
        material['tool_receipts'] = sorted(receipts)
        fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        self.meta['no_progress'] = self.meta['no_progress'] + 1 if fingerprint == self.meta.get('fingerprint') else 1
        self.meta['fingerprint'] = fingerprint
        if self.meta['no_progress'] >= self.policy['max_no_progress']:
            return self.stop(result, 'workflow_no_progress')
        if self.remaining() < 1:
            return self.stop(result, 'workflow_deadline_exhausted')
        if self.meta['turns'] >= self.policy['max_turns']:
            return self.stop(result, 'workflow_turn_limit')
        self.meta.update(state='running', phase='checkpoint')
        return None

    def stop(self, result, reason):
        prior = result.get('outcome')
        outcome = {**(prior if isinstance(prior, dict) else {}), 'status': 'blocked', 'reason': reason}
        for key in ('evidence', 'errors', 'remaining_work'):
            items = outcome.get(key)
            outcome[key] = [item for item in items if isinstance(item, str) and item.strip()] if isinstance(items, list) else []
        outcome['remaining_work'] = outcome['remaining_work'] or ['Workflow needs reconciliation before continuing']
        self.meta.update(state='blocked', phase='finished', outcome=outcome,
                         finished_at=datetime.now(timezone.utc).isoformat(),
                         outcomeRecordedAt=datetime.now(timezone.utc).isoformat())
        self.meta['blocked_message'] = 'Autonomous workflow blocked (' + reason + ').\n' + result.get('answer', '')
        return {**result, 'answer': self.meta['blocked_message'],
                'outcome': outcome}

    def continuation(self, *, compact=False):
        prefix = ((f'Child wait finished; resume coordinator turn {self.meta["turns"]}.\n'
                   if self.meta.get('awaiting_children') else
                   f'Coordinator turn {self.meta["turns"]} finished; begin turn {self.meta["turns"] + 1}.\n') +
                  'Continue the same authorized task from the saved checkpoint. Do not repeat completed '
                  'actions, implementation, publications or completed child tasks. Reconcile uncertain effects '
                  'before any write. Finish missing review/QA and read back requested publication/CI results. '
                  'Do not request confirmation already granted. Return blocked for a real user decision, '
                  'missing permission or unsafe recovery. Respond in the original user language.\n' +
                  ('Original authorization remains in the server-provided scope and saved checkpoint.\n'
                   if compact else 'Original task and authorization (unchanged):\n' + self.meta['original_task']) +
                  '\nSaved reported evidence and remaining work (verify before relying on it):\n')
        space = RUNTIME['limits']['message_chars'] - len(prefix)
        if space < 200:
            raise ValueError('workflow continuation context exceeds the message limit')
        return prefix + json.dumps(self.meta['outcome'], ensure_ascii=False)[:space]


def run_history(saved, current):
    config = (saved or {}).get('roleConfig') or {}
    history = list(config.get('_workflow_runs', []))
    previous = config.get('_workflow')
    if previous and previous.get('run_id') != current.meta['run_id']:
        entry = {k:previous.get(k) for k in ('run_id','started_at','finished_at','deadlineAt','state','turns')}
        entry['run_id'] = entry['run_id'] or 'legacy-' + (saved or {})['sessionId']
        entry['reason'] = previous.get('outcome', {}).get('reason')
        history = [item for item in history if item.get('run_id') != entry['run_id']] + [entry]
    return history[-20:]


def recover_workflows(server, policy, token):
    """Resume only durable completed-turn checkpoints, never an uncertain send."""
    from threading import Thread, current_thread
    from urllib import request, error
    import sys
    if getattr(server, '_workflow_closing', False):
        return
    if time.monotonic() < getattr(server, '_workflow_recover_at', 0):
        return
    server._workflow_recover_at = time.monotonic() + RUNTIME['sessions']['prune_interval_seconds']
    try:
        saved_chats = server.store.list_chats()
    except OSError:
        return

    def resume(saved):
        sid = saved['sessionId']
        try:
            workflow = WorkflowRun(role_policy(saved['roleConfig'], policy, stored=True), '', saved['roleConfig']['_workflow'])
            host, port = server.server_address[:2]
            host = '127.0.0.1' if host == '0.0.0.0' else host
            body = json.dumps({'sessionId': sid, 'message': workflow.continuation()}).encode()
            call = request.Request(f'http://{host}:{port}/messages', data=body,
                headers={'Content-Type':'application/json', 'X-Popot-Workflow-Recovery':token})
            with request.urlopen(call, timeout=max(1, workflow.remaining()) + 30) as response:
                response.read()
        except (OSError, ValueError, error.HTTPError) as exc:
            # The executing request owns its failure state. Never replay an uncertain send.
            print(json.dumps({'event': 'workflow_recovery_error', 'session_id': sid,
                              'error_type': type(exc).__name__}), file=sys.stderr, flush=True)
        finally:
            with server.live_lock:
                server._workflow_recovering.discard(sid)
                server._workflow_threads.discard(current_thread())

    for saved in saved_chats:
        meta = (saved.get('roleConfig') or {}).get('_workflow')
        if not meta or meta.get('state') != 'running':
            continue
        selected_policy = role_policy(saved.get('roleConfig'), policy, stored=True)
        if selected_policy is None:
            continue
        sid = saved['sessionId']
        lock = server.session_lock(sid)
        if not lock.acquire(blocking=False):
            continue
        try:
            with server.live_lock:
                if sid in server._workflow_recovering:
                    continue
            workflow = WorkflowRun(selected_policy, '', meta)
            if meta.get('phase') != 'checkpoint' or not isinstance(meta.get('checkpoint'), dict):
                workflow.stop({'answer':'API restarted during execution; reconcile external effects before continuing.',
                               'outcome':meta.get('outcome')}, 'workflow_interrupted_execution')
                server.store.save(sid, saved['agent'], saved['messages'], saved['role'],
                                  {**saved['roleConfig'], '_workflow': workflow.meta})
            elif workflow.remaining() < 1 or meta['turns'] >= policy['max_turns']:
                workflow.stop({'answer':''}, 'workflow_budget_exhausted')
                server.store.save(sid, saved['agent'], saved['messages'], saved['role'],
                                  {**saved['roleConfig'], '_workflow': workflow.meta})
            else:
                with server.live_lock:
                    if len(server._workflow_recovering) >= policy['max_concurrent']:
                        continue
                    server._workflow_recovering.add(sid)
                    thread = Thread(target=resume, args=(saved,), name='workflow-recovery-' + sid, daemon=True)
                    server._workflow_threads.add(thread)
                    thread.start()
        finally:
            lock.release()
