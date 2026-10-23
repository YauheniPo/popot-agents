"""OpenAI-compatible chat harness with a bounded local tool loop."""

import hashlib
import json
import math
import os
import sys
import time
import uuid
from copy import deepcopy
from urllib import error, request
from urllib.parse import urlsplit

from popot_agents.prompts import render_prompt
from popot_agents.runtime_config import RUNTIME, validate_model_parameters
from popot_agents.progress import trace_event, trace_output
from popot_agents.skills import prepare_role_config
from popot_agents.tools import TOOL_SCHEMAS, ToolExecutionError, ToolInputError, execute_tool
from . import mcp_client
from .delegation_policy import (available_roles, completed_answer, delegation_instructions,
                                handoff_task)
from .turn_memory import TurnMemory, parse_summary
from .final_result import evidence_transcript, parse_result, retained_receipts
from .review_progress import ReviewProgress, NAME as REVIEW_CHECKPOINT, SCHEMA as REVIEW_SCHEMA


def _trace(event: str, **fields) -> None:
    trace_event(event, **fields)


def _trace_arguments(name: str, arguments: dict) -> dict:
    if name == "calculate":
        return {"expression": arguments.get("expression")}
    if name == "utc_time":
        return {}
    if name == "read_file":
        return {"path": arguments.get("path")}
    if name == "write_file":
        return {"path": arguments.get("path"),
                "content_chars": len(arguments["content"]) if isinstance(arguments.get("content"), str) else None}
    if name in {"git_clone", "download_file"}:
        try:
            host = urlsplit(arguments.get("url", "")).hostname
        except (ValueError, TypeError, AttributeError):
            host = None
        return {"url_host": host,
                **{key: arguments.get(key) for key in ("directory", "path") if key in arguments}}
    if name == "bash":
        command = arguments.get('command')
        return {"command_chars": len(command) if isinstance(command, str) else None,
                "command_sha256": hashlib.sha256(command.encode()).hexdigest() if isinstance(command, str) else None}
    return {"argument_keys": sorted(arguments)}


class _FinalAnswerRequired(RuntimeError):
    pass


class _ModelRequestError(RuntimeError):
    def __init__(self, message, *, retryable):
        super().__init__(message)
        self.retryable = retryable


def _model_request(call, *, round_number, messages, schemas, remaining_seconds, final=False):
    """Retry only completion generation; tools run after a complete response."""
    attempts = 1 if final else 1 + RUNTIME["model"]["request_retries"]
    for attempt in range(1, attempts + 1):
        request_timeout = min(RUNTIME["timeouts"]["model_request_seconds"], remaining_seconds())
        started_at = time.monotonic()
        _trace("model_request", round=round_number, attempt=attempt, message_count=len(messages),
               timeout_seconds=round(request_timeout, 3),
               available_tools=[schema["function"]["name"] for schema in schemas])
        trace_output(f"LLM request started round={round_number} attempt={attempt}")
        retryable = False
        try:
            with request.urlopen(call, timeout=request_timeout) as response:
                result = json.load(response)
        except error.HTTPError as exc:
            code = exc.code
            exc.close()
            failure = f"model endpoint returned HTTP {code}"
            retryable = code in {502, 503, 504}
            _trace("model_http_error", round=round_number, attempt=attempt, status=code)
        except (TimeoutError, error.URLError) as exc:
            if isinstance(exc, TimeoutError) or isinstance(exc.reason, TimeoutError):
                elapsed = time.monotonic() - started_at
                _trace("model_timeout", round=round_number, attempt=attempt,
                       elapsed_seconds=round(elapsed, 1), timeout_seconds=round(request_timeout, 3))
                failure = (f"model request timed out (round={round_number}, "
                           f"socket timeout={request_timeout:.1f}s, elapsed={elapsed:.1f}s, "
                           f"attempt={attempt}/{attempts})")
                retryable = True
            else:
                failure = "model endpoint is unreachable"
        else:
            trace_output(f"LLM response received after {time.monotonic() - started_at:.1f}s")
            return result
        if retryable and not final:
            remaining_seconds()  # Preserve the reserved final-answer budget, even on the last attempt.
        if not retryable or attempt == attempts:
            raise _ModelRequestError(failure, retryable=retryable) from None
        remaining = remaining_seconds()
        _trace("model_retry", round=round_number, next_attempt=attempt + 1,
               reason=failure, remaining_seconds=round(remaining, 3))


def run_http(task: str | list[dict[str, str]], role_config: dict | None = None,
             *, extra_tools: dict | None = None, timeout_seconds: float | None = None,
             state: dict | None = None) -> str:
    turn_timeout = float(os.getenv('HARNESS_TURN_TIMEOUT_SECONDS',
                                  str(RUNTIME['timeouts']['turn_seconds'])))
    if not math.isfinite(turn_timeout) or turn_timeout <= 0:
        raise ValueError('HARNESS_TURN_TIMEOUT_SECONDS must be positive and finite')
    if timeout_seconds is not None:
        if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be positive and finite')
        turn_timeout = min(turn_timeout, timeout_seconds)
    messages = [{'role': 'user', 'content': task}] if isinstance(task, str) else list(task)
    role_config = prepare_role_config(role_config)
    deadline = None
    if timeout_seconds is not None or role_config.get('harness2', {}).get('enabled'):
        deadline = time.monotonic() + turn_timeout
    memory = TurnMemory(role_config, messages)
    memory.start_session()
    success = False
    try:
        if role_config.get('harness2', {}).get('enabled') and not (state and state.get('messages')):
            from .harness2 import refine_role
            role_config = refine_role(messages, role_config, deadline=deadline,
                                      tool_names=list(extra_tools or {}))
        answer = _run_http(task, role_config, extra_tools=extra_tools, memory=memory,
                           deadline=deadline, turn_timeout=turn_timeout, state=state)
        success = memory.run_complete
        return answer
    finally:
        memory.finish_session(success)


def _run_http(task: str | list[dict[str, str]], role_config: dict | None = None,
             *, extra_tools: dict | None = None, memory=None, deadline=None, turn_timeout=None,
             state: dict | None = None) -> str:
    base_url = os.environ.get("HARNESS_BASE_URL", "").rstrip("/")
    model = os.environ.get("HARNESS_MODEL", "")
    if not base_url.startswith(("http://", "https://")) or not model:
        raise ValueError("HARNESS_BASE_URL and HARNESS_MODEL are required")
    if turn_timeout is None:
        turn_timeout = float(os.getenv("HARNESS_TURN_TIMEOUT_SECONDS",
                                       str(RUNTIME["timeouts"]["turn_seconds"])))
    if turn_timeout <= 0:
        raise ValueError("HARNESS_TURN_TIMEOUT_SECONDS must be positive")
    if deadline is None:
        deadline = time.monotonic() + turn_timeout

    def remaining_seconds() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("turn deadline exceeded")
        return remaining

    messages = [{"role": "user", "content": task}] if isinstance(task, str) else list(task)
    user_context = list(messages)
    resuming = bool(state and state.get('messages') and
                    state.get('outcome', {}).get('status') in {'partial', 'blocked', 'failed'})
    if resuming:
        messages = [*state['messages'], messages[-1]]
    extra_tools = dict(extra_tools or {})
    if 'delegate_task' in extra_tools:
        # This scope controls the HTTP parent's lifecycle, not the child's task API.
        delegation = dict(extra_tools['delegate_task'])
        delegation['schema'] = deepcopy(delegation['schema'])
        delegation['schema']['function']['parameters']['properties']['scope'] = {
            'type': 'string', 'enum': ['subtask', 'whole_task'],
            'description': render_prompt('delegation/scope')}
        extra_tools['delegate_task'] = delegation
    instructions = role_config.get("instructions", "")
    allowed = role_config.get("tools", [])
    mcp_servers = role_config.get("mcpServers", {})
    mcp_tools = (mcp_client.discover_tools(mcp_servers, timeout_seconds=remaining_seconds())
                 if mcp_servers else {})
    # Writes use the curated runtime capture, not arbitrary model-selected text.
    mcp_tools.pop('mcp__cognee__remember', None)
    reviewer = role_config.get('name', role_config.get('_ax_role')) == 'code_reviewer'
    review_progress = ReviewProgress(state.get('review_progress') if resuming else None) if reviewer else None
    if review_progress:
        extra_tools[REVIEW_CHECKPOINT] = {'schema': REVIEW_SCHEMA, 'call': review_progress.record}
    schemas = [TOOL_SCHEMAS[name] for name in allowed]
    schemas.extend(item["schema"] for item in mcp_tools.values())
    if mcp_tools and 'read_tool_output' not in allowed:
        allowed = [*allowed, 'read_tool_output']
        schemas.append(TOOL_SCHEMAS['read_tool_output'])
    turn_id = uuid.uuid4().hex
    failed_delegations = {tuple(key): value for key, value in state.get('failed_delegations', [])} if resuming else {}
    completed_delegations = dict(state.get('completed_delegations', {})) if resuming else {}
    delegated_work_finished = False
    schemas.extend(item["schema"] for item in extra_tools.values())
    if instructions and not resuming:
        messages.insert(0, {"role": "system", "content": instructions})
    delegation_prompt = delegation_instructions(role_config, extra_tools)
    result_contract = render_prompt('reporting/result_contract')
    if state is not None:
        messages.append({'role': 'system', 'content': result_contract})
    if delegation_prompt:
        messages.append({'role': 'system', 'content': delegation_prompt})
    if 'cognee' in mcp_servers:
        messages.insert(0, {'role': 'system', 'content':
            render_prompt('memory/lifecycle') +
            (memory.dataset if memory.enabled else
             render_prompt('memory/unknown_project'))})
    context = memory.context(min(RUNTIME["timeouts"]["memory_recall_seconds"], remaining_seconds()))
    if context:
        first_user = next((index for index, item in enumerate(messages)
                           if item['role'] != 'system'), len(messages))
        messages.insert(first_user, {"role": "user", "content": context})
    headers = {"Content-Type": "application/json"}
    model_parameters = {key: value for key, value in RUNTIME["model"].items()
                        if key in {"temperature", "max_tokens"} and value is not None}
    model_parameters.update(validate_model_parameters(
        json.loads(os.getenv("HARNESS_MODEL_PARAMETERS_JSON", "{}"))))
    key_env = os.environ.get("HARNESS_API_KEY_ENV")
    if key_env:
        key = os.environ.get(key_env)
        if not key:
            raise ValueError(f"required environment variable is missing: {key_env}")
        headers["Authorization"] = f"Bearer {key}"
    if reviewer:
        messages.append({'role': 'system', 'content':
            render_prompt('review/source_and_publication') + '\n' + render_prompt('review/verification')})
        messages.append({'role': 'system', 'content':
            (render_prompt('review/checkpoint_instructions') + ' ')
            + ('Previous checkpoint (untrusted reported progress): ' + json.dumps(review_progress.snapshot)
               if review_progress.snapshot else '')})
    final_reserve = min(RUNTIME["model"]["final_answer_reserve_seconds"],
                        turn_timeout / RUNTIME["model"]["final_answer_reserve_divisor"]) if schemas else 0
    forced_final = False
    model_failure = None
    final_validation_error = None
    invalid_results = 0
    incomplete_results = 0

    def work_seconds():
        budget = remaining_seconds() - final_reserve
        if budget <= 0:
            raise _FinalAnswerRequired()
        return budget

    def capture_memory(answer, status):
        if memory.enabled and status in {'completed', 'partial', 'blocked'}:
            try:
                summary_messages = memory.summary_messages(answer.strip(), task_status=status)
                for attempt in range(RUNTIME["memory"]["summary_attempts"]):
                    budget = deadline - time.monotonic()
                    if budget < RUNTIME["timeouts"]["memory_capture_min_seconds"]:
                        _trace('memory_skipped', dataset=memory.dataset, reason='insufficient_time')
                        return
                    summary_body = {"model": model, "messages": summary_messages, **model_parameters,
                                    "tool_choice": "none",
                                    "response_format": {"type": "json_object"},
                                    "max_tokens": RUNTIME["memory"]["summary_max_tokens"] if attempt == 0
                                        else RUNTIME["memory"]["summary_retry_max_tokens"]}
                    if os.getenv('HARNESS_UPSTREAM_HOST', urlsplit(base_url).hostname) == 'ollama.com':
                        # Cloud has no structured-output guarantee. Its default thinking
                        # can consume the extraction token budget before emitting JSON.
                        summary_body.pop('response_format', None)
                        summary_body.pop('reasoning', None)
                        summary_body.update(reasoning_effort='none', temperature=0)
                    summary_call = request.Request(f"{base_url}/chat/completions",
                        data=json.dumps(summary_body).encode(), headers=headers, method="POST")
                    # Retry only invalid extraction output; never repeat a memory write or task.
                    with request.urlopen(summary_call, timeout=min(RUNTIME["timeouts"]["memory_summary_seconds"],
                        budget - RUNTIME["timeouts"]["memory_capture_margin_seconds"])) as response:
                        extraction = json.load(response)
                    memory.record_usage(extraction)
                    retry_reason = 'invalid_summary_shape'
                    try:
                        choice = extraction['choices'][0]
                        content = choice['message'].get('content')
                        reasoning = choice['message'].get('reasoning') or choice['message'].get('reasoning_content')
                        finish_reason = choice.get('finish_reason')
                        _trace('memory_summary_response', dataset=memory.dataset, attempt=attempt + 1,
                               finish_reason=finish_reason if finish_reason in (
                                   'stop', 'length', 'tool_calls', 'content_filter', 'function_call') else 'unknown',
                               content_chars=len(content) if isinstance(content, str) else None,
                               reasoning_chars=len(reasoning) if isinstance(reasoning, str) else 0)
                        if finish_reason not in (None, 'stop'):
                            retry_reason = 'truncated_summary' if finish_reason == 'length' else 'unexpected_finish_reason'
                            raise ValueError('Memory summary generation did not finish normally')
                        summary = parse_summary(content)
                        break
                    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                        if attempt + 1 == RUNTIME["memory"]["summary_attempts"]:
                            raise
                        _trace('memory_summary_retry', dataset=memory.dataset,
                               reason=retry_reason)
                        summary_messages.append({'role': 'system', 'content':
                            render_prompt('memory/retry')})
                memory.client.timeout = min(RUNTIME["timeouts"]["memory_request_seconds"], remaining_seconds() / 2)
                memory.save(summary, task_status=status)
            except Exception as exc:
                _trace('memory_capture_failed', dataset=memory.dataset, error_type=type(exc).__name__)

    def save_result(answer, status, reason, report=None):
        outcome = {'status': status, 'reason': reason,
            **({key: list(dict.fromkeys(item.strip() for item in report[key]
                    if isinstance(item, str) and item.strip()))
                for key in ('evidence', 'remaining_work', 'errors')}
               if report else {'evidence': [], 'remaining_work': [],
                               'errors': [value['error'] for value in failed_delegations.values()]
                                   + ([model_failure] if model_failure else [])})}
        if status != 'completed' and not outcome['remaining_work']:
            outcome['remaining_work'] = ['Task completeness is unverified']
        if state is not None:
            checkpoint = {'messages': [*messages, {'role': 'assistant', 'content': answer}],
                          'failed_delegations': [[list(key), value] for key, value in failed_delegations.items()],
                          'completed_delegations': completed_delegations if status != 'completed' else {},
                          'outcome': outcome}
            if review_progress and review_progress.snapshot is not None:
                checkpoint['review_progress'] = review_progress.snapshot
            if status == 'completed':
                checkpoint['messages'] = []
            if len(json.dumps(checkpoint, ensure_ascii=False).encode()) > RUNTIME['limits']['history_bytes']:
                raise RuntimeError('Harness checkpoint exceeds the history limit; sandbox must not restart with empty context')
            state.clear()
            state.update(checkpoint)
        capture_memory(answer, status)
        return answer

    def partial_answer(answer='', report=None):
        memory.run_complete = False
        if report is None:
            receipts = retained_receipts(messages, completed_delegations) if state is not None else []
            report = {'evidence': receipts, 'remaining_work': [],
                      'errors': [e for e in (model_failure, final_validation_error) if e]}
            if receipts:
                answer = (answer.strip() + '\n\n' if answer else '') + ('Retained evidence '
                    '(not a completion claim):\n' + '\n'.join(receipts))
        blocked_child = any(value.get('status') == 'blocked' for value in failed_delegations.values())
        incomplete_child = failed_delegations and not forced_final and not model_failure
        if failed_delegations:
            # Full child answers stay in the checkpoint/tool history. Do not recursively
            # copy nested transcripts into the user-facing answer at every parent level.
            summaries = []
            for value in failed_delegations.values():
                summary = f"{value['role']} · sessionId: {value.get('session_id')} · {value.get('status') or 'incomplete'}"
                summary += '\n' + value['error'][:400]
                for key in ('evidence', 'remaining_work'):
                    items = value.get('outcome', {}).get(key, [])
                    if isinstance(items, list):
                        detail = '; '.join(
                            item.strip()[:400] for item in items[:4] if isinstance(item, str) and item.strip())
                        if detail:
                            summary += '\n' + key + ': ' + detail
                if (value.get('answer') and len(value['answer']) <= 600
                        and '\nIncomplete subtasks:' not in value['answer']):
                    summary += '\n' + value['answer']
                elif not value.get('outcome', {}).get('evidence') and value.get('answer'):
                    summary += '\n' + value['answer'].split('\nIncomplete subtasks:', 1)[0][:600]
                summaries.append(summary)
            answer = (answer.strip() + '\n\n' if answer else '') + 'Incomplete subtasks:\n' + '\n'.join(summaries)
            report = {key: list(report[key]) if report else []
                      for key in ('evidence', 'remaining_work', 'errors')}
            for value in failed_delegations.values():
                if value['error'] not in report['errors']:
                    report['errors'].append(value['error'])
                for key in ('evidence', 'remaining_work', 'errors'):
                    items = value.get('outcome', {}).get(key, [])
                    if isinstance(items, list):
                        report[key].extend(item for item in items if isinstance(item, str) and item.strip()
                                           and item not in report[key])
            if not report['remaining_work']:
                report['remaining_work'] = ['Task completeness is unverified']
        status = ('Partial result: a delegated task is blocked; resolve its reported precondition.'
                  if blocked_child else
                  'Partial result: the model returned an invalid final result; inspect the saved evidence.'
                  if invalid_results else
                  'Partial result: the model request failed; task completeness is unverified.'
                  if model_failure else
                  'Partial result: a delegated task is incomplete. Inspect its evidence before retrying.'
                  if incomplete_child else
                  'Partial result: work budget exhausted. '
                  'Task completeness is unverified. External actions may have persisted; '
                  'inspect their results before retrying.')
        return save_result(status + ('\n\n' + answer.strip() if answer else ''),
                           'blocked' if blocked_child else 'partial',
                           'delegated_task_blocked' if blocked_child else
                           'invalid_final_result' if invalid_results else
                           'model_request_failed' if model_failure else
                           'delegated_task_incomplete' if incomplete_child else 'work_budget_exhausted', report)

    def completion(round_number, final):
        request_messages = messages
        if review_progress and review_progress.snapshot and not final:
            request_messages = [*messages, {'role': 'system', 'content': review_progress.focus()}]
        if checkpoint_only and not final:
            request_messages = [*request_messages, {'role': 'system', 'content':
                render_prompt('review/checkpoint_request')}]
        if final:
            # End the tool conversation. Some providers keep emitting calls from
            # its history even with tool_choice=none; retain that history as data.
            request_messages = [{'role': 'system', 'content':
                render_prompt('reporting/final', result_contract=result_contract if state is not None else "")},
                {'role': 'user', 'content': json.dumps(
                    evidence_transcript(messages, context=user_context), ensure_ascii=False)}]
        body = {"model": model, "messages": request_messages, **model_parameters}
        selected_schemas = [] if final else schemas
        if checkpoint_only and not final:
            selected_schemas = [REVIEW_SCHEMA]
            body['tool_choice'] = {'type': 'function', 'function': {'name': REVIEW_CHECKPOINT}}
        if selected_schemas:
            body['tools'] = selected_schemas
        if final:
            body['tool_choice'] = 'none'
            if os.getenv('HARNESS_UPSTREAM_HOST', urlsplit(base_url).hostname) == 'ollama.com':
                # As with memory extraction, use content budget for the JSON deliverable.
                body.pop('reasoning', None)
                body.pop('response_format', None)
                body.update(reasoning_effort='none', temperature=0)
        call = request.Request(f"{base_url}/chat/completions",
            data=json.dumps(body).encode('utf-8'), headers=headers, method='POST')
        return _model_request(call, round_number=round_number, messages=request_messages,
            schemas=selected_schemas, remaining_seconds=remaining_seconds if final else work_seconds,
            final=final)

    max_tool_rounds = role_config.get('max_tool_rounds', RUNTIME['model']['default_max_tool_rounds'])
    for round_number in range(1, max_tool_rounds + 3):
        # Leave a useful model-call window, including on short resumed turns.
        forced_final = bool(invalid_results or delegated_work_finished or review_progress and review_progress.finished or round_number > max_tool_rounds
                            or final_reserve and remaining_seconds() <= final_reserve + min(30, turn_timeout / 2))
        checkpoint_only = bool(review_progress and review_progress.due and not forced_final)
        try:
            try:
                result = completion(round_number, forced_final)
            except (_FinalAnswerRequired, _ModelRequestError) as exc:
                if isinstance(exc, _ModelRequestError):
                    if forced_final or not schemas or not exc.retryable:
                        raise
                    model_failure = str(exc)
                    _trace('model_final_fallback', round=round_number, reason=model_failure)
                forced_final = True
                result = completion(round_number, True)
        except RuntimeError as exc:
            if forced_final and (isinstance(exc, _ModelRequestError) and exc.retryable
                                 or 'timed out' in str(exc) or 'deadline exceeded' in str(exc)):
                _trace('turn_partial', reason='final_model_timeout', round=round_number)
                return partial_answer()
            raise
        memory.record_usage(result)
        if forced_final and time.monotonic() >= deadline:
            return partial_answer()
        remaining_seconds()
        try:
            message = result["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("model endpoint returned no answer") from exc
        calls = message.get("tool_calls") or []
        _trace("model_response", round=round_number, tool_calls=len(calls))
        reasoning_fields = {name: message[name] for name in
                            ('reasoning', 'reasoning_content', 'reasoning_details') if message.get(name)}
        if reasoning_fields:
            # Trace a redacted copy; preserve provider blocks unchanged in the
            # assistant message sent back after tool execution.
            _trace('assistant_reasoning', round=round_number, **reasoning_fields)
        if calls and forced_final:
            _trace('turn_partial', reason='final_model_requested_tools', round=round_number)
            return partial_answer()
        if calls:
            if review_progress and not checkpoint_only:
                review_progress.rounds += 1
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                _trace("assistant_message", round=round_number, content=content)
            messages.append({'role': 'assistant', **message})
            for tool_call in calls:
                remaining_seconds()
                name = None
                arguments = {}
                delegation_key = None
                completed_child = None
                delegation_succeeded = False
                delegation_scope = 'subtask'
                tool_started_at = time.monotonic()
                try:
                    function = tool_call["function"]
                    name = function["name"]
                    call_id = tool_call["id"]
                    if not isinstance(name, str) or not isinstance(call_id, str) or not call_id:
                        raise ValueError("tool name and call ID must be nonempty strings")
                    if review_progress and review_progress.finished:
                        raise ToolInputError('Review work ended at its checkpoint. Return the final answer.')
                    if delegated_work_finished:
                        raise ToolInputError('The whole delegated task completed. Return the final answer.')
                    if checkpoint_only and name != REVIEW_CHECKPOINT:
                        raise ToolInputError('Only review_checkpoint is available in this round.')
                    if name not in extra_tools and name not in mcp_tools and name not in allowed:
                        names = sorted(set(extra_tools) | set(mcp_tools) | set(allowed))
                        raise ToolInputError(f"tool is not allowed: {name}. Available tools: {', '.join(names)}")
                    try:
                        arguments = json.loads(function["arguments"])
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise ToolInputError("tool arguments must be valid JSON encoding an object") from exc
                    if not isinstance(arguments, dict):
                        raise ToolInputError("tool arguments must be an object")
                    if remaining_seconds() <= final_reserve:
                        raise ToolInputError('Tool not executed: time reserved for the final answer.')
                    _trace("tool_call", round=round_number, name=name,
                           call_id=tool_call["id"],
                           arguments=_trace_arguments(name, arguments))
                    if name in extra_tools:
                        if name == 'delegate_task':
                            delegation_scope = arguments.pop('scope', 'subtask')
                            if delegation_scope not in ('subtask', 'whole_task'):
                                raise ToolInputError('delegate_task scope must be subtask or whole_task')
                            if arguments.get('role') not in available_roles(extra_tools):
                                raise ToolInputError('delegate_task requires an available specialist role')
                            if not isinstance(arguments.get('task'), str) or not arguments['task'].strip():
                                raise ToolInputError('delegate_task requires a nonempty task')
                            session_id = arguments.get('session_id')
                            completed_child = (completed_delegations.get(session_id)
                                               if isinstance(session_id, str) else None)
                            if completed_child and completed_child['role'] != arguments['role']:
                                completed_child = None
                            # Only a successful retry of the same role AND task
                            # can resolve its failure. Another job for that role
                            # (even with a reused model call ID) proves nothing.
                            delegation_key = (arguments['role'], arguments['task'].strip())
                            if not completed_child and work_seconds() < 60:
                                if delegation_key in failed_delegations or any(
                                        value.get('session_id') == session_id
                                        for value in failed_delegations.values() if value.get('session_id')):
                                    delegation_key = None  # No attempt; preserve the existing checkpoint.
                                raise ToolInputError('Child not started: delegation needs at least 60 seconds '
                                                     'of work budget. Return evidence and remaining work.')
                            replacement = arguments.pop('replaces_session_id', None)
                            if replacement is not None:
                                replaced = next((key for key, value in failed_delegations.items()
                                    if key[0] == arguments['role'] and value.get('session_id') == replacement), None)
                                if (not isinstance(replacement, str) or replaced is None
                                        or arguments.get('session_id')
                                        or failed_delegations[replaced].get('resumable')):
                                    delegation_key = None
                                    raise ToolInputError('Replacement requires a known non-resumable failed child '
                                                         'of the same role. Continue resumable work with session_id.')
                                delegation_key = replaced
                            previous = next((value for key, value in failed_delegations.items()
                                if key[0] == arguments['role'] and value.get('resumable')), {})
                            if previous.get('resumable') and not arguments.get('session_id'):
                                delegation_key = None
                                raise ToolInputError('Continue the unfinished child using role and session_id=' +
                                                     previous['session_id'] + '; do not create a replacement sandbox')
                            task_text = arguments['task']
                            prior = failed_delegations.get(delegation_key)
                            if prior:
                                task_text += (('\n\n' + render_prompt('delegation/previous_attempt') + '\n')
                                    + json.dumps(prior, ensure_ascii=False))
                            arguments = {**arguments, 'task': handoff_task(
                                task_text, user_context, role_config)}
                        if completed_child:
                            delegation_key = None
                            output = json.dumps({**completed_child, 'continuation_skipped': True,
                                'notice': 'This session already completed; no new work was executed. '
                                          'This is its previous verified result. For a new requirement or '
                                          'fresh verification, start a new task without session_id.'}, ensure_ascii=False)
                            _trace('delegation_result_reused', session_id=completed_child['sessionId'])
                            delegation_succeeded = True
                        else:
                            output = extra_tools[name]["call"](
                                arguments, timeout_seconds=work_seconds(),
                                call_id=turn_id + ":" + tool_call["id"])
                            if name == REVIEW_CHECKPOINT and review_progress:
                                _trace('review_checkpoint',
                                       covered_files=len(review_progress.snapshot['covered_files']),
                                       decisions=len(review_progress.snapshot['decisions']),
                                       remaining_work=len(review_progress.snapshot['remaining_work']),
                                       tool_work_finished=review_progress.finished)
                    elif name in mcp_tools:
                        selected = mcp_tools[name]
                        review_action = None
                        if reviewer and selected['server'] == 'github' \
                                and selected['native_name'] == 'pull_request_review_write':
                            review_action = arguments.get('method')
                        if reviewer and selected['server'] == 'github' \
                                and selected['native_name'] == 'add_comment_to_pending_review':
                            review_action = 'comment'
                        if review_action is not None:
                            owner, repo, number = (arguments.get(key) for key in ('owner', 'repo', 'pullNumber'))
                            if not isinstance(owner, str) or not owner.strip() \
                                    or not isinstance(repo, str) or not repo.strip() \
                                    or type(number) is not int or number <= 0:
                                raise ToolInputError('Review operations require owner, repo and a positive pullNumber.')
                            if review_action == 'comment' and (arguments.get('subjectType') != 'LINE'
                                    or type(arguments.get('line')) is not int or arguments['line'] <= 0
                                    or arguments.get('side') not in ('LEFT', 'RIGHT')):
                                raise ToolInputError('Inline findings require subjectType=LINE, a positive diff line '
                                                     'and side=LEFT or RIGHT. Verify the position against the actual diff.')
                        if selected['server'] == 'cognee':
                            if not memory.enabled:
                                raise ToolInputError('Memory needs one repository URL or [project: slug].')
                            if arguments.get('dataset_name', memory.dataset) != memory.dataset:
                                raise ToolInputError('Memory is scoped to this task project: ' + memory.dataset)
                            arguments['dataset_name'] = memory.dataset
                        try:
                            output = mcp_client.call_tool(
                                mcp_servers[selected["server"]], selected["native_name"], arguments,
                                timeout_seconds=work_seconds())
                        except RuntimeError as exc:
                            if selected['server'] != 'cognee':
                                raise
                            raise mcp_client.MCPToolError(
                                'Project memory is unavailable; continue using current sources.') from None
                    else:
                        output = execute_tool(name, arguments, allowed,
                                              timeout_seconds=work_seconds())
                except (ToolInputError, ToolExecutionError, mcp_client.MCPToolError) as exc:
                    _trace("tool_error", round=round_number, name=name, call_id=call_id,
                           error_type=type(exc).__name__,
                           error=('bash execution failed; details retained in tool result'
                                  if name == 'bash' and isinstance(exc, ToolExecutionError) else str(exc)),
                           recoverable=True)
                    output = json.dumps({
                        "isError": True, "error": str(exc),
                        **({'outcome_unknown': True} if isinstance(exc, mcp_client.MCPTransportError) else {}),
                        "next_step": ("Correct the arguments or choose another permitted step. "
                                     "Inspect any partial effects before repeating a write. "
                                     "Do not claim success without verification."),
                    }, ensure_ascii=False)
                except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                    _trace("tool_error", round=round_number, name=name,
                           error_type=type(exc).__name__)
                    raise RuntimeError(f"invalid tool call: {exc}") from exc
                except Exception as exc:
                    _trace("tool_error", round=round_number, name=name,
                           error_type=type(exc).__name__)
                    raise
                if delegation_key is not None:
                    try:
                        completed_answer(output, delegation_key[0], session_id=arguments.get('session_id'))
                    except RuntimeError as exc:
                        try:
                            child = json.loads(output)
                        except (TypeError, ValueError):
                            child = {}
                        child_session = arguments.get('session_id') or (
                            child.get('sessionId') if isinstance(child, dict) else None)
                        previous = {}
                        if arguments.get('session_id'):
                            for key, value in list(failed_delegations.items()):
                                if key[0] == delegation_key[0] and value.get('session_id') == child_session:
                                    previous = failed_delegations.pop(key)
                        failure = {**previous, 'error': str(exc),
                            'role': delegation_key[0],
                            'session_id': child_session,
                            'status': child.get('outcome', {}).get('status') if isinstance(child, dict)
                                and isinstance(child.get('outcome'), dict) else None,
                            'resumable': isinstance(child, dict) and child.get('resumable') is True}
                        if isinstance(child, dict) and child.get('role') == delegation_key[0] \
                                and child.get('sessionId') == child_session and isinstance(child.get('answer'), str) \
                                and child['answer'].strip():
                            failure['answer'] = child['answer']
                            failure['outcome'] = child['outcome'] if isinstance(child.get('outcome'), dict) else {}
                        failed_delegations[delegation_key] = failure
                    else:
                        completed_child = json.loads(output)
                        delegation_succeeded = True
                        completed_delegations[completed_child['sessionId']] = completed_child
                        failed_delegations.pop(delegation_key, None)
                        session_id = arguments.get('session_id')
                        if session_id:
                            failed_delegations = {key: value for key, value in failed_delegations.items()
                                if not (key[0] == delegation_key[0] and value.get('session_id') == session_id)}
                if (name == 'delegate_task' and delegation_scope == 'whole_task' and delegation_succeeded
                        and not failed_delegations
                        and isinstance(completed_child.get('outcome'), dict)
                        and completed_child['outcome'].get('status') == 'completed'
                        and isinstance(completed_child['outcome'].get('evidence'), list)
                        and any(isinstance(item, str) and item.strip()
                                for item in completed_child['outcome']['evidence'])):
                    delegated_work_finished = True
                    _trace('delegated_work_completed', session_id=completed_child['sessionId'])
                result_fields = ({"output": output} if name in {"calculate", "utc_time"}
                                 else {"output_chars": len(output)})
                if role_config.get('_ax_harness2_probe'):
                    result_fields = {'output': output[:4000], 'output_chars': len(output)}
                _trace("tool_result", round=round_number, name=name, call_id=call_id,
                       elapsed_seconds=round(time.monotonic() - tool_started_at, 1),
                       **result_fields)
                messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
                memory.observe(call_id, name, arguments, output)
            continue
        answer = message.get("content")
        report = None
        if state is not None:
            finish_reason = result['choices'][0].get('finish_reason')
            try:
                report = parse_result(answer, finish_reason)
                answer = report['answer']
            except ValueError as exc:
                invalid_results += 1
                reason = str(exc)
                _trace('final_result_invalid', round=round_number, attempt=invalid_results,
                       reason=reason, content_chars=len(answer) if isinstance(answer, str) else 0,
                       finish_reason=finish_reason if finish_reason in
                           ('stop', 'length', 'tool_calls', 'content_filter') else 'unknown')
                # Preserve even the failed correction in the resumable checkpoint.
                messages.append({'role': 'assistant', **message})
                final_validation_error = 'model returned an invalid final result: ' + reason
                if invalid_results >= 2 or deadline - time.monotonic() <= 0:
                    return partial_answer()
                messages.append({'role': 'system', 'content':
                    render_prompt('reporting/repair', reason=reason, result_contract=result_contract)})
                continue
            if invalid_results:
                final_validation_error = None
                _trace('final_result_recovered', round=round_number)
        elif not isinstance(answer, str) or not answer.strip():
            if forced_final:
                return partial_answer()
            raise RuntimeError("model endpoint returned an empty answer")
        if report and report['status'] in {'blocked', 'failed'}:
            memory.run_complete = False
            return save_result(answer, report['status'], 'agent_reported', report)
        if failed_delegations:
            if report and report['status'] == 'partial':
                return partial_answer(answer, report)
            if forced_final:
                return partial_answer()
            if not any(value.get('resumable') for value in failed_delegations.values()):
                return partial_answer()
            incomplete_results += 1
            if incomplete_results >= 2:
                return partial_answer()
            messages.append({'role': 'system', 'content':
                (render_prompt('delegation/incomplete_result') + '\n') +
                json.dumps(list(failed_delegations.values()), ensure_ascii=False)})
            continue
        _trace("assistant_answer", round=round_number, content=answer.strip())
        remaining_seconds()
        if (delegated_work_finished or review_progress and review_progress.finished) and report and report['status'] == 'partial':
            memory.run_complete = False
            return save_result(answer, 'partial', 'agent_reported', report)
        if forced_final and not (report and report['status'] == 'completed'
                                 and report['evidence'] and not model_failure):
            _trace('turn_partial', reason='work_budget_exhausted',
                   round=round_number)
            return partial_answer(answer, report)
        if report and report['status'] == 'partial':
            memory.run_complete = False
            return save_result(answer, 'partial', 'agent_reported', report)
        return save_result(answer.strip(), 'completed', 'answer_returned', report)
    return partial_answer()


def main() -> None:
    try:
        role_config = json.loads(os.getenv("HARNESS_ROLE_JSON", "{}"))
        print(run_http(sys.stdin.read(), role_config), flush=True)
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"HTTP harness failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
