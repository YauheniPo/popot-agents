"""Guide model-selected delegation using user context and available capabilities."""

import json
import re

from popot_agents.prompts import render_prompt
from popot_agents.runtime_config import RUNTIME
from popot_agents.tools import ToolInputError
from .turn_memory import scrub


class DelegationContextError(ToolInputError):
    code = 'delegation_context_limit'
    retryable = False


def publication_policy(role_config):
    instructions = role_config.get('instructions', '')
    marker = 'Repository publication policy:'
    if marker not in instructions:
        return ''
    policy = instructions.split(marker, 1)[1].split('# Selected skill references', 1)[0]
    return marker + ' ' + policy.split('Runtime manages Cognee memory;', 1)[0].strip()


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
        review_routing = render_prompt('delegation/reviewer')
    elif role == 'qa_engineer':
        review_routing = render_prompt('delegation/qa')
    elif 'code_reviewer' in roles:
        review_routing = render_prompt('delegation/review_routing')
    return render_prompt('delegation/instructions', role=role,
                         review_routing=review_routing, description=description)


def handoff_task(task: str, messages: list[dict], role_config: dict) -> str:
    """Carry user authority independently of what the parent model remembered to pass."""
    context = [{'role': m['role'], 'content': scrub(m['content'])}
               for m in messages if m.get('role') == 'user']
    previous_answer = next((m['content'] for m in reversed(messages)
                            if m.get('role') == 'assistant'), None)
    latest_user = next((m['content'] for m in reversed(messages) if m.get('role') == 'user'), '')
    handoff = (render_prompt('delegation/latest_request')
               if task == latest_user else scrub(task))
    # Persisted conversation contains original user requests and prior answers, not tool evidence.
    def render(items):
        return render_prompt('delegation/handoff', handoff=handoff,
                             context=json.dumps(items, ensure_ascii=False)) + '\n' + publication_policy(role_config)
    content = render(context)
    limit = RUNTIME['limits']['message_chars']
    if len(content) > limit:
        raise DelegationContextError('Delegation authorization and assignment exceed the message limit. '
            'This cannot be retried by shrinking an optional prior answer; preserve restrictions and restructure the input.')
    # Prior model prose is optional context, never authority. Fit its JSON
    # escaping within the remaining budget rather than displacing user scope.
    if previous_answer:
        text = scrub(previous_answer)[:8000]
        while text:
            candidate = render([*context, {'role':'assistant', 'content':text,
                'note':'Unverified prior answer; explicitly shortened to fit the context budget.'}])
            if len(candidate) <= limit:
                content = candidate
                break
            text = text[:max(0, len(text)-(len(candidate)-limit))]
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
