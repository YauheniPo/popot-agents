"""Bounded observations of actual tool results, distinct from task acceptance."""
import hashlib
import json
import os
import re
import shlex
from datetime import datetime, timezone

from popot_agents.progress import redact_text

MAX_RECEIPTS = 16
_SHA = r'[a-fA-F0-9]{40}'
_NAME = r'[A-Za-z0-9_.-]+'
_PULL_REQUEST_URL = re.compile(r'github\.com/(?:repos/)?(' + _NAME + r')/(' + _NAME + r')/(?:pull|pulls)/(\d+)')
_SHELLS = {'bash', 'dash', 'sh', 'zsh'}
_WRAPPERS = {'command', 'env', 'exec', 'nohup', 'sudo', 'time'}
_OPERATORS = {'&&', '||', ';', '|', '&'}
_GIT_GLOBAL_OPTIONS = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--super-prefix'}
_GIT_GLOBAL_FLAGS = {'--no-pager', '-P', '--paginate', '--bare'}
_REV_PARSE_FLAGS = {'--verify', '-q', '--quiet', '--'}
_HEAD_REVISIONS = (['HEAD'], ['HEAD^{commit}'])
_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=\S*')


def _tokenize(command):
    """Shell tokens with unquoted operators separated and quoted text kept intact."""
    lexer = shlex.shlex(command or '', posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = '#'
    try:
        return list(lexer)
    except ValueError:
        return []


def _line_commands(line):
    commands, current, skip_target = [], [], False
    for token in _tokenize(line):
        if skip_target:
            skip_target = False
            continue
        if token in _OPERATORS or token and set(token) <= set('();|&'):
            if current:
                commands.append(current)
            current = []
        elif re.fullmatch(r'\d?[<>]+&?', token):
            if current and current[-1].isdigit():
                current.pop()
            skip_target = True
        elif re.fullmatch(r'\d?[<>]+&?\S+', token):
            continue
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def _lines(command):
    """Physical lines, except that a quoted or escaped newline stays in its line."""
    lines, current, quote, escaped = [], [], None, False
    for char in command or '':
        if escaped:
            current.append(char)
            escaped = False
        elif quote != "'" and char == '\\':
            current.append(char)
            escaped = True
        elif quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
            current.append(char)
        elif char == '\n':
            lines.append(''.join(current))
            current = []
        else:
            current.append(char)
    lines.append(''.join(current))
    return lines


_HEREDOC_WORD = re.compile(r'[A-Za-z0-9_.-]+')


def _heredocs(line):
    """Heredoc redirections of one physical line as (word, strip_tabs) pairs."""
    found, quote, index = [], None, 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == '\\' and quote == '"':
                index += 1
            elif char == quote:
                quote = None
        elif char == '\\':
            index += 1
        elif char == '#' and (index == 0 or line[index - 1] in ' \t'):
            break
        elif char in {'"', "'"}:
            quote = char
        elif line.startswith('<<', index):
            if line.startswith('<<<', index):
                index += 3
                continue
            index += 2
            strip_tabs = line.startswith('-', index)
            index += 1 if strip_tabs else 0
            while index < len(line) and line[index] in ' \t':
                index += 1
            if line[index:index + 1] == '\\':
                index += 1
            closer = line[index] if line[index:index + 1] in {'"', "'"} else None
            if closer:
                end = line.find(closer, index + 1)
                word = line[index + 1:end] if end != -1 else ''
                index = end + 1 if end != -1 else len(line)
            else:
                match = _HEREDOC_WORD.match(line, index)
                word, index = (match[0], match.end()) if match else ('', len(line))
            if not word:
                break
            found.append((word, strip_tabs))
            continue
        index += 1
    return found


def _shell_commands(command):
    """Split a command line into simple commands without breaking quoted arguments."""
    commands, pending = [], []
    for line in _lines(command):
        if pending:
            word, strip_tabs = pending[0]
            if (line.lstrip('\t') if strip_tabs else line) == word:
                pending.pop(0)
            continue
        commands.extend(_line_commands(line))
        pending.extend(_heredocs(line))
    return commands


def _head_read(tokens):
    while tokens:
        name = os.path.basename(tokens[0])
        if name in _SHELLS:
            if len(tokens) > 2 and re.fullmatch(r'-[A-Za-z]*c', tokens[1]):
                return _rev_parse_head(tokens[2])
            return False
        if name not in _WRAPPERS:
            break
        tokens = tokens[1:]
        while tokens and (tokens[0].startswith('-') or _ASSIGNMENT.fullmatch(tokens[0])):
            tokens = tokens[1:]
    while tokens and _ASSIGNMENT.fullmatch(tokens[0]):
        tokens = tokens[1:]
    if not tokens or os.path.basename(tokens[0]) != 'git':
        return False
    rest = tokens[1:]
    while rest:
        if rest[0] in _GIT_GLOBAL_OPTIONS and len(rest) > 1:
            rest = rest[2:]
        elif rest[0] in _GIT_GLOBAL_FLAGS or rest[0].startswith('--') and '=' in rest[0]:
            rest = rest[1:]
        else:
            break
    if not rest or rest[0] != 'rev-parse':
        return False
    arguments = rest[1:]
    revisions = [a for a in arguments if not a.startswith('-')]
    flags = {a for a in arguments if a.startswith('-')}
    return revisions in _HEAD_REVISIONS and flags <= _REV_PARSE_FLAGS


def _rev_parse_head(command):
    """True only when a shell segment really invokes `git rev-parse HEAD`."""
    return any(_head_read(tokens) for tokens in _shell_commands(command))


def _pull_reference(data, arguments):
    """Repository and number of the pull request a github call or payload describes."""
    arguments = arguments if isinstance(arguments, dict) else {}
    owner, repo, number = arguments.get('owner'), arguments.get('repo'), arguments.get('pullNumber')
    named = (isinstance(owner, str) and isinstance(repo, str) and re.fullmatch(_NAME, owner)
             and re.fullmatch(_NAME, repo) and type(number) is int and number > 0)
    base = data.get('base') if isinstance(data.get('base'), dict) else {}
    payload_repo = base.get('repo') if isinstance(base.get('repo'), dict) else {}
    full_name = payload_repo.get('full_name')
    if not named and isinstance(full_name, str) and full_name.count('/') == 1:
        owner, repo, number = full_name.split('/')[0], full_name.split('/')[1], data.get('number')
        named = bool(owner and repo and type(number) is int and number > 0)
    if named:
        return f'{owner}/{repo}'.lower(), number
    for link in (data.get('html_url'), data.get('url')):
        match = _PULL_REQUEST_URL.search(link) if isinstance(link, str) else None
        if match:
            return f'{match[1]}/{match[2]}'.lower(), int(match[3])
    return None


def observe_tool_result(name, arguments, output, call_id):
    if name in {'delegate_task', 'review_checkpoint', 'read_tool_output'}:
        return None
    text = redact_text(output)
    exit_match = re.match(r'exit_code=(-?\d+)\n', text)
    body = text[exit_match.end():] if exit_match else text
    try:
        data, _ = json.JSONDecoder().raw_decode(body.lstrip())
    except (ValueError, TypeError):
        data = {}
    data = data if isinstance(data, dict) else {}
    command = arguments.get('command', '') if name == 'bash' else ''
    command = command if isinstance(command, str) else ''
    exit_code = int(exit_match[1]) if exit_match else None
    error = bool(data.get('isError') or data.get('error') or exit_code not in (None, 0))
    status, kind, summary = 'observed', None, ''
    revision = None
    if name == 'bash':
        checks = re.search(r'Ran \d+ tests?(?: in [\d.]+s)?|\d+ passed(?:, \d+ failed)?|FAILED \([^\n)]{0,80}\)|# pass \d+', text)
        if checks and re.search(r'\b(?:pytest|unittest)\b|\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b|\bnode\b[^\n]*--test', command):
            kind, summary = 'checks', checks[0].strip()
            failed_checks = re.search(r'\b[1-9]\d* (?:failed|errors?)\b|FAILED \(|(?m:^EXIT_CODE=[1-9]\d*\s*$)', text)
            if error or failed_checks:
                status = 'failed'
            elif exit_code == 0 and re.search(r'(?m)(?:^OK(?:\s|$))|(?:\d+ passed)|(?:# pass \d+)', text):
                status = 'passed'
        elif exit_code == 0 and re.search(r'\bgit\b[^\n;|&]*\bcommit\b', command) and re.search(r'(?m)^\[[^\]\n]+ [a-f0-9]{7,40}\]', text):
            kind, summary = 'commit', 'Git commit returned successfully; publication is separate.'
        elif exit_code == 0 and re.search(r'\bgit\b[^\n;|&]*\bpush\b', command) and re.search(r'(?m)^\s+\S+\s+\S+\s+->\s+\S+', text):
            kind, summary = 'publication', 'Git push returned a remote-ref update; verify the remote head and CI.'
        if _rev_parse_head(command):
            match = re.search(r'(?m)^('+_SHA+r')\s*$', text)
            revision = match[1].lower() if match else None
            if revision and kind is None:
                kind, summary = 'revision', 'Git HEAD read-back observed.'
    if 'github' in name or name == 'bash' and exit_code == 0:
        head = data.get('head', {})
        sha = head.get('sha') if isinstance(head, dict) else head
        sha = sha or data.get('headRefOid')
        if name.endswith('get_commit'):
            sha = data.get('sha')
        if isinstance(sha, str) and re.fullmatch(_SHA, sha):
            revision = sha.lower()
            kind, summary = kind or 'revision', summary or 'Revision read-back observed in tool result.'
        if re.search(r'"conclusion"\s*:', text):
            kind, summary = 'ci', 'GitHub check-run results observed; inspect individual conclusions.'
            records = data.get('check_runs', data.get('statusCheckRollup', []))
            records = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []
            failed = [str(r.get('name','check'))[:80] for r in records
                      if str(r.get('conclusion','')).lower() in {'failure','error','timed_out','action_required','cancelled'}]
            if failed:
                status, summary = 'failed', 'CI failures: ' + ', '.join(failed[:4])
            elif records and all(str(r.get('conclusion','')).lower() in {'success','neutral','skipped'} for r in records):
                status, summary = 'passed', 'All returned CI checks finished successfully, neutral or skipped.'
            elif records:
                summary = 'CI pending: ' + ', '.join(str(r.get('name','check'))[:80] for r in records
                    if not r.get('conclusion'))[:250]
    if error:
        kind = kind or 'blocker'
        timed_out = name == 'bash' and str(data.get('error', '')).startswith('command timed out')
        status = 'unknown' if data.get('outcome_unknown') or timed_out else 'failed'
        summary = (f'exit_code={exit_code}; {summary}' if kind == 'checks' else
                   ('Shell command timed out; reconcile effects using the saved tool output.' if timed_out
                    else 'Shell execution failed; inspect the saved tool output.')
                   if name == 'bash' and 'partial output' in str(data.get('error', ''))
                   else str(data.get('error') or f'Tool returned exit code {exit_code}')[:320])
    if kind is None:
        return None
    receipt = {'callId':str(call_id)[:128], 'tool':name[:160], 'kind':kind, 'status':status,
        'source':'tool_result', 'observedAt':datetime.now(timezone.utc).isoformat(),
        'outputSha256':hashlib.sha256(output.encode()).hexdigest(),
        'summary':summary[:320]}
    if revision:
        receipt['revision'] = revision
        if data.get('headRefOid') or isinstance(data.get('head'),dict) and data['head'].get('sha'):
            receipt['revisionSource'] = 'pr_head'
            pull = _pull_reference(data, arguments)
            if pull:
                receipt['repository'], receipt['pullRequest'] = pull
    if exit_code is not None:
        receipt['exitCode'] = exit_code
    if command:
        receipt['command'] = redact_text(command)[:512]
        receipt['commandTruncated'] = len(command) > 512
        receipt['commandSha256'] = hashlib.sha256(command.encode()).hexdigest()
    if status == 'unknown':
        receipt['outcomeUnknown'] = True
    artifact = re.search(r'Full output: (\.tool-results/[a-f0-9]{32}\.txt)', text)
    if artifact:
        receipt['outputArtifact'] = artifact[1]
    refs = re.findall(r'https://github\.com/[\w.-]+/[\w.-]+/(?:commit/[a-f0-9]{40}|pull/\d+|actions/runs/\d+(?:/job/\d+)?)', text)
    if refs:
        receipt['references'] = list(dict.fromkeys(refs))[:4]
    return receipt


def retain_evidence(existing, receipt):
    # The call ID deduplicates progress forwarded through several coordinators.
    key = (receipt['callId'], receipt['observedAt'])
    return [r for r in existing if (r.get('callId'), r.get('observedAt')) != key][-MAX_RECEIPTS+1:] + [receipt]


def evidence_event(message):
    """Accept only the small receipt contract, never arbitrary trace payloads."""
    try:
        event = json.loads(message)
    except (ValueError, TypeError):
        return None
    value = event.get('receipt') if isinstance(event, dict) and event.get('event') == 'work_evidence' else None
    if not isinstance(value, dict) or value.get('source') != 'tool_result':
        return None
    if value.get('kind') not in {'commit','publication','revision','checks','ci','blocker'} or value.get('status') not in {'observed','passed','failed','unknown'}:
        return None
    result = {}
    for key, size in (('callId',128),('tool',160),('kind',32),('status',32),('source',32),
                      ('summary',320),('observedAt',64),('command',512),('commandSha256',64),('outputSha256',64),('runId',64)):
        if isinstance(value.get(key), str):
            result[key] = redact_text(value[key])[:size]
    if not all(result.get(k) for k in ('callId','tool','observedAt','summary')):
        return None
    if isinstance(value.get('revision'), str) and re.fullmatch(_SHA, value['revision']):
        result['revision'] = value['revision'].lower()
    if value.get('revisionSource') == 'pr_head':
        result['revisionSource'] = 'pr_head'
        if isinstance(value.get('repository'), str) and re.fullmatch(_NAME + '/' + _NAME, value['repository']):
            result['repository'] = value['repository'].lower()
        if type(value.get('pullRequest')) is int and value['pullRequest'] > 0:
            result['pullRequest'] = value['pullRequest']
    if type(value.get('exitCode')) is int:
        result['exitCode'] = value['exitCode']
    if value.get('outcomeUnknown') is True:
        result['outcomeUnknown'] = True
    if value.get('commandTruncated') is True:
        result['commandTruncated'] = True
    if isinstance(value.get('outputArtifact'), str) and re.fullmatch(r'\.tool-results/[a-f0-9]{32}\.txt', value['outputArtifact']):
        result['outputArtifact'] = value['outputArtifact']
    if isinstance(value.get('references'), list):
        result['references'] = [ref for ref in value['references'][:4] if isinstance(ref, str)
            and len(ref) <= 400 and re.fullmatch(r'https://github\.com/[\w./-]+', ref)]
    return result


def reusable_checks(receipts, revision):
    """No revision or a different revision is never a reusable test result."""
    if not isinstance(revision, str) or not re.fullmatch(_SHA, revision):
        return []
    return [r for r in receipts if isinstance(r, dict) and r.get('source') == 'tool_result'
            and r.get('kind') == 'checks' and r.get('status') == 'passed'
            and r.get('revision') == revision.lower() and r.get('exitCode') == 0]
