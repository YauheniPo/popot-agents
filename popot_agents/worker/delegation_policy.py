"""Route specialist requests using user context and actual delegation capabilities."""

import json
import re

from popot_agents.runtime_config import RUNTIME
from popot_agents.tools import ToolInputError
from .turn_memory import scrub


PR_URL = re.compile(r'https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*(?![0-9])')
REVIEW = re.compile(r'code[\s-]*review|\breview\b|ревью|ревь?ю', re.I)
CHECK = re.compile(r'проверь|\bcheck\b|\binspect\b', re.I)
PR_REFERENCE = re.compile(r'\bPR\b|pull\s+request|пул[ -]?реквест', re.I)
COMMENT = re.compile(r'комментар|замечани|\bcomments?\b|\bfindings?\b', re.I)
PUBLISH = re.compile(r'оставь|добавь|опубликуй|размести|\bpost\b|\bpublish\b|\bsubmit\b|\badd\b|\bleave\b', re.I)
IMPLEMENT = re.compile(r'\bimplement\b|\bfix\b|\bchange\b|\bedit\b|исправь|реализуй|измени|внеси|почини', re.I)
EXPLICIT = re.compile(r'delegate_task|\bdelegate\b|делегируй|передай|поручи|вызови', re.I)
NO_DELEGATION = re.compile(
    r'\b(?:do not|don\x27t|never)\s+(?:delegate|use\s+delegate_task|call\s+delegate_task)'
    r'|не\s+(?:делегируй|передавай|вызывай|используй\s+delegate_task)', re.I)


def implementation_requested(text: str) -> bool:
    for action in IMPLEMENT.finditer(text):
        clause = re.split(r'[.!?;]|\b(?:but|however|но|однако)\b',
                          text[:action.start()], flags=re.I)[-1]
        if not re.search(r'\b(?:do not|don\x27t|never|no|не)\b', clause, re.I):
            return True
    return False


def available_roles(extra_tools: dict) -> list[str]:
    tool = extra_tools.get('delegate_task', {})
    return tool.get('schema', {}).get('function', {}).get('parameters', {}).get(
        'properties', {}).get('role', {}).get('enum', [])


def required_specialist(messages: list[dict], role_config: dict) -> str | None:
    users = [m['content'] for m in messages if m.get('role') == 'user']
    if not users:
        return None
    current = users[-1]
    intent = re.sub(r'https?://\S+', '', current)
    allowed = role_config.get('allowed_roles', [])
    if (NO_DELEGATION.search(intent) or re.search(
            r'\bhow (?:do|to|can)\b|как\s+(?:мне\s+)?(?:сделать|оставить|опубликовать)', intent, re.I)):
        return None
    if EXPLICIT.search(intent):
        named = [role for role in allowed if re.search(r'\b' + re.escape(role) + r'\b', intent)]
        if len(named) == 1:
            return named[0]
    if role_config.get('name', role_config.get('_ax_role')) == 'code_reviewer':
        return None
    if 'code_reviewer' not in allowed or implementation_requested(intent):
        return None
    has_pr = any(PR_URL.search(text) for text in users)
    if has_pr and (REVIEW.search(intent)
                   or CHECK.search(intent) and (PR_URL.search(current) or PR_REFERENCE.search(intent))
                   or COMMENT.search(intent) and PUBLISH.search(intent)):
        return 'code_reviewer'
    return None


def delegation_instructions(role_config: dict, extra_tools: dict) -> str:
    roles = available_roles(extra_tools)
    if not roles:
        return ''
    description = extra_tools['delegate_task']['schema']['function'].get('description', '')
    return (
        f"You are {role_config.get('name', role_config.get('_ax_role', 'the parent agent'))}. "
        'Use delegate_task to execute work belonging to an available specialist, including '
        'PR reviews and review comments through code_reviewer. Make a real tool call; '
        'writing a suggested task or instructions for the user is not delegation. '
        'Do not substitute create_pull_request, local scripts or old bot comments for a review. '
        'Give the child concrete inputs, requirements, actual evidence and user restrictions. '
        'The runtime also carries the user conversation and repository publication policy. '
        'Child workspaces are separate; a parent-local path is not readable by a child. '
        'Treat a child error, empty answer or truncated result as incomplete. '
        'TASK_STATE_COMPLETED confirms an answer returned, not that an external action succeeded. '
        'Report published actions only with the child\'s verified result and URLs; '
        'do not invent findings or repeat a publication when its outcome is uncertain. '
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


def completed_answer(output: str, role: str) -> str:
    try:
        result = json.loads(output)
    except (TypeError, ValueError):
        raise RuntimeError(f'delegation to {role} returned invalid JSON') from None
    if not isinstance(result, dict):
        raise RuntimeError(f'delegation to {role} returned an invalid result')
    session = result.get('sessionId')
    suffix = f' (sessionId={session})' if isinstance(session, str) and re.fullmatch(r'[0-9a-f]{16}', session) else ''
    answer = result.get('answer')
    if (result.get('isError') or result.get('error') or result.get('state') != 'TASK_STATE_COMPLETED'
            or result.get('role') != role or result.get('truncated')
            or not isinstance(answer, str) or not answer.strip() or not suffix):
        raise RuntimeError(f'delegation to {role} did not return a complete successful answer{suffix}; '
                           'inspect the child result before retrying external actions')
    return f'{role} · sessionId: {session}\n\n{answer.strip()}'
