"""Guide model-selected delegation using user context and available capabilities."""

import json
import os

from popot_agents.child_results import completed_answer  # Compatibility import for existing callers.
from popot_agents.prompts import render_prompt
from popot_agents.runtime_config import RUNTIME
from popot_agents.tools import ToolInputError
from popot_agents.work_evidence import executed_commands
from .turn_memory import scrub


class DelegationContextError(ToolInputError):
    code = 'delegation_context_limit'
    retryable = False


def _publication_command(words):
    """Identify git/gh writes after global options, never from argument text."""
    program = os.path.basename(words[0])
    rest = words[1:]
    operands = ({'-c', '-C', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--config-env'}
                if program == 'git' else {'-R', '--repo', '--hostname'})
    flags = {'--no-pager', '-P', '--paginate', '-p', '--bare', '--no-replace-objects',
             '--literal-pathspecs', '--glob-pathspecs', '--noglob-pathspecs',
             '--icase-pathspecs', '--no-optional-locks', '--no-lazy-fetch'}
    while rest and rest[0].startswith('-'):
        option, separator, _ = rest[0].partition('=')
        if option in operands:
            rest = rest[1 if separator else 2:]
        elif program == 'git' and (rest[0] in flags or separator and option == '--exec-path'
                                  or rest[0][:2] in {'-c', '-C'} and len(rest[0]) > 2):
            rest = rest[1:]
        elif program == 'gh' and rest[0].startswith('-R') and len(rest[0]) > 2:
            rest = rest[1:]
        else:
            return None
    if program == 'git' and rest and rest[0] in {'commit', 'commit-tree', 'push'}:
        return rest[0]
    if program == 'gh' and rest[:2] in [['pr', 'create'], ['pr', 'merge']]:
        return 'push'
    return None


def check_publication_owner(name, arguments, context):
    """Coordinate branch publication; role credentials remain the security boundary."""
    writes = ({_publication_command(words) for words in executed_commands(arguments.get('command'))}
              if name == 'bash' else set())
    if context and context.get('role') == 'code_reviewer':
        if writes & {'commit', 'commit-tree'}:
            raise ToolInputError('The code reviewer cannot create commits; delegate authorized fixes to backend_engineer.')
    if context is None or 'publication_owner' not in context or context.get('publication_owner') == context.get('session_id'):
        return
    branch_write = name.rsplit('__',1)[-1] in {
        'push_files','create_or_update_file','delete_file','create_branch',
        'create_pull_request','merge_pull_request','update_pull_request_branch'}
    if name == 'bash':
        branch_write = 'push' in writes
    if branch_write:
        raise ToolInputError('Branch publication belongs to integration owner '+
            str(context.get('publication_owner') or 'not assigned')+
            '; return commits or patches and check evidence for integration. Original authorization still applies.')


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
    elif role == 'security_engineer':
        review_routing = render_prompt('delegation/security')
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
