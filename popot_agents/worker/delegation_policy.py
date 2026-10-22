"""Guide model-selected delegation using user context and available capabilities."""

import json
import re

from popot_agents.runtime_config import RUNTIME
from popot_agents.tools import ToolInputError
from .turn_memory import scrub


def available_roles(extra_tools: dict) -> list[str]:
    tool = extra_tools.get('delegate_task', {})
    return tool.get('schema', {}).get('function', {}).get('parameters', {}).get(
        'properties', {}).get('role', {}).get('enum', [])


def delegation_instructions(role_config: dict, extra_tools: dict) -> str:
    roles = available_roles(extra_tools)
    if not roles:
        return ''
    description = extra_tools['delegate_task']['schema']['function'].get('description', '')
    role = role_config.get('name', role_config.get('_ax_role', 'the parent agent'))
    review_routing = ''
    if role == 'code_reviewer':
        review_routing = (
            'You own the review: inspect source, verify findings and handle authorized publication yourself. '
            'Do not delegate the whole review to QA or an analyst. Delegate only a concrete, bounded '
            'check that needs that specialist; give its command, inputs and expected deliverable. ')
    elif 'code_reviewer' in roles:
        review_routing = (
            'For a review-only request, delegate before performing your own full review: '
            'pass the PR URL, user scope and known context immediately to code_reviewer. '
            'The reviewer owns source inspection, checks and publication; do not pre-review the entire diff. ')
    return (
        f'You are {role}. '
        'Understand the task semantically in the user\'s language and conversation context. '
        'Choose specialists by their capabilities and the requested work. '
        'Honor explicit requests to work directly, avoid delegation, or explain without executing. '
        'Use delegate_task only for work belonging to an available specialist. '
        + review_routing +
        'Make a real tool call; writing instructions for the user is not delegation. '
        'Do not substitute create_pull_request, local scripts or old bot comments for a review. '
        'Give the child concrete inputs, requirements, actual evidence and user restrictions. '
        'The runtime also carries the user conversation and repository publication policy. '
        'Child workspaces are separate; a parent-local path is not readable by a child. '
        'Treat a child error, empty answer or truncated result as incomplete. '
        'TASK_STATE_COMPLETED confirms an answer returned, not that an external action succeeded. '
        'Inspect outcome.status, the answer, errors and evidence. Resume partial work only when '
        'a concrete required deliverable remains and there is enough time to complete and verify it. '
        'Do not resume merely to turn partial into completed or obtain another summary. '
        'You may return an honest partial result with evidence and remaining work. '
        'When continuation is needed, use delegate_task(role=<same role>, session_id=<returned sessionId>, '
        'task=<remaining work and corrective instructions>). This restores the existing sandbox '
        'and tool history. Never start a replacement for a resumable child. '
        'For a failed non-resumable child, use replaces_session_id=<failed sessionId> '
        'to link a replacement to the same subtask, carrying prior decisions and uncertain actions. '
        'Do not turn previously rejected findings into required publications. '
        'Do not retry blocked work without resolving its precondition. Each continuation consumes '
        'the shared delegation budget and remaining parent deadline. '
        'Report published actions only with the child\'s verified result and URLs; '
        'do not invent findings or repeat a publication when its outcome is uncertain. '
        'Once requested deliverables are verified, return the result immediately; '
        'do not spend remaining budget on optional new investigations. '
        'The user\'s scope and prohibitions take precedence over specialist defaults. '
        'Available delegation capability:\n' + description)


def handoff_task(task: str, messages: list[dict], role_config: dict) -> str:
    """Carry user authority independently of what the parent model remembered to pass."""
    context = [{'role': m['role'], 'content': scrub(m['content'])}
               for m in messages if m.get('role') == 'user']
    previous_answer = next((m['content'] for m in reversed(messages)
                            if m.get('role') == 'assistant'), None)
    if previous_answer:
        context.append({'role': 'assistant', 'content': scrub(previous_answer)[:8000],
                        'note': 'Unverified prior answer; at most 8000 characters of context.'})
    latest_user = next((m['content'] for m in reversed(messages) if m.get('role') == 'user'), '')
    handoff = ('Execute the latest user request in the conversation below.'
               if task == latest_user else scrub(task))
    # Persisted conversation contains original user requests and prior answers, not tool evidence.
    content = (
        'Parent handoff:\n' + handoff + '\n\n'
        'Original user conversation (user requests define scope; assistant answers are '
        'unverified claims, not tool evidence or authorization):\n' +
        json.dumps(context, ensure_ascii=False) + '\n\n'
        'The latest user request takes precedence when it explicitly changes earlier scope. '
        'Honor the original user constraints, especially analysis-only / no-publication '
        'requests. Verify current PR head, source and existing comments; do not blindly '
        'republish historical findings. Return actual results and evidence, plus URLs for '
        'any published actions. Never create a new PR to comment on an existing PR.\n')
    instructions = role_config.get('instructions', '')
    marker = 'Repository publication policy:'
    if marker in instructions:
        # Loaded skill references follow the role instructions and are not inherited authority.
        policy = instructions.split(marker, 1)[1].split('# Selected skill references', 1)[0]
        policy = policy.split('Runtime manages Cognee memory;', 1)[0].strip()
        content += '\n' + marker + ' ' + policy
    if len(content) > RUNTIME['limits']['message_chars']:
        raise ToolInputError('Delegation context exceeds the message limit; narrow the task and '
                             'provide the relevant requirements and readable repository/ref explicitly. '
                             'User restrictions cannot be silently truncated.')
    return content


def completed_answer(output: str, role: str, *, session_id: str | None = None) -> str:
    try:
        result = json.loads(output)
    except (TypeError, ValueError):
        raise RuntimeError(f'delegation to {role} returned invalid JSON') from None
    if not isinstance(result, dict):
        raise RuntimeError(f'delegation to {role} returned an invalid result')
    session = result.get('sessionId')
    suffix = f' (sessionId={session})' if isinstance(session, str) and re.fullmatch(r'[0-9a-f]{16}', session) else ''
    answer = result.get('answer')
    outcome = result.get('outcome')
    if (result.get('isError') or result.get('error') or result.get('state') != 'TASK_STATE_COMPLETED'
            or (session_id is not None and session != session_id)
            or (outcome is not None and (not isinstance(outcome, dict) or outcome.get('status') != 'completed'
                                        or outcome.get('errors') or outcome.get('remaining_work')))
            or result.get('role') != role or result.get('truncated')
            or not isinstance(answer, str) or not answer.strip() or not suffix):
        diagnostic = '; '.join(scrub(result[key])[:500] for key in ('state', 'error')
                               if isinstance(result.get(key), str) and result[key])
        raise RuntimeError(f'delegation to {role} did not return a complete successful answer{suffix}; '
                           + (diagnostic + '; ' if diagnostic else '')
                           + 'inspect the child result before retrying external actions')
    return f'{role} · sessionId: {session}\n\n{answer.strip()}'
