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
_WRAPPERS = {'command', 'env', 'exec', 'nice', 'nohup', 'sudo', 'time', 'timeout'}
# Options a wrapper accepts before its own command: flags that take no operand,
# options that take one, and options that make the wrapper skip the command.
# Anything outside a grammar fails closed: the guard must not guess a command ran.
_WRAPPER_OPTIONS = {
    'command': ({'-p', '--'}, {}, {'-v', '-V'}),
    'env': ({'-i', '--ignore-environment', '-v', '--debug', '--'},
            {'-u': 1, '--unset': 1, '-C': 1, '--chdir': 1, '-S': 1, '--split-string': 1},
            {'-0', '--null'}),
    'exec': ({'-c', '-l', '--'}, {'-a': 1}, set()),
    'nohup': (set(), set(), {'--help', '--version'}),
    'sudo': ({'-A', '--askpass', '-B', '--bell', '-b', '--background', '-E', '--preserve-env',
              '-H', '--set-home', '-i', '--login', '-K', '--remove-timestamp', '-k',
              '--reset-timestamp', '-N', '--no-update', '-n', '--non-interactive', '-P',
              '--preserve-groups', '-S', '--stdin', '-s', '--shell', '--'},
             {'-a': 1, '--auth-type': 1, '-c': 1, '--login-class': 1, '-C': 1, '-D': 1,
              '--chdir': 1, '--close-from': 1, '--command-timeout': 1, '-g': 1,
              '--group': 1, '-h': 1, '--host': 1, '-p': 1, '--preserve-env': 1,
              '--prompt': 1, '-R': 1, '--chroot': 1, '-r': 1, '--role': 1, '-T': 1,
              '-t': 1, '--type': 1, '-U': 1, '--other-user': 1, '-u': 1, '--user': 1},
             {'-e', '--edit', '-l', '--list', '-v', '--validate', '-V', '--version'}),
    'time': ({'-p', '--portability'}, {}, set()),
}
# GNU `time` is also a binary with its own grammar when the command names a path.
_GNU_TIME_OPTIONS = ({'-a', '--append', '-v', '--verbose', '-q', '--quiet', '--'},
                     {'-f': 1, '--format': 1, '-o': 1, '--output': 1}, set())
_ASSIGNING_WRAPPERS = {'env', 'sudo'}
_OPERATORS = {'&&', '||', ';', '|', '&'}
_GIT_GLOBAL_OPTIONS = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--super-prefix'}
_DURATION = re.compile(r'\d+(?:\.\d+)?[smhd]?')
_INTEGER = re.compile(r'[+-]?\d+')
_GIT_GLOBAL_FLAGS = {'--no-pager', '-P', '--paginate', '--bare'}
_REV_PARSE_FLAGS = {'--verify', '-q', '--quiet', '--'}
_HEAD_WORDS = ('HEAD', 'HEAD^{commit}')
# Reserved words that can precede a simple command inside one shell segment.
_RESERVED = {'!', '{', '}', 'if', 'then', 'elif', 'else', 'fi', 'for', 'while',
             'until', 'do', 'done', 'case', 'esac', 'select', 'function'}
_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=\S*')


_QUOTED = '\x01'
_QUOTE_END = '\x02'
_ESCAPED = {}
_ESCAPE_VALUES = {}


def _placeholder(char):
    """A word character standing in for an escaped character until tokenizing."""
    if char not in _ESCAPED:
        placeholder = chr(0xE000 + len(_ESCAPED))
        _ESCAPED[char] = placeholder
        _ESCAPE_VALUES[placeholder] = char
    return _ESCAPED[char]


class _Word(str):
    """A shell word that remembers which parts quoting or escaping produced."""

    quoted = False
    name_quoted = False


def _marked(command):
    """Command with markers inside quoted words and stand-ins for escaped characters."""
    pieces, quote, index = [], None, 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == quote:
                pieces.append(_QUOTE_END + char)
                quote = None
            elif char == '\\' and quote == '"' and index + 1 < len(command):
                index += 1
                pieces.append('\\' + command[index])
            else:
                pieces.append(char)
        elif char in {'"', "'"}:
            quote = char
            pieces.append(char + _QUOTED)
        elif char == '\\' and index + 1 < len(command):
            index += 1
            escaped = command[index]
            # An escaped newline is a continuation the shell removes; every other
            # escaped character is a literal that cannot end the word or start one.
            pieces.append('\\\n' if escaped == '\n' else _placeholder(escaped))
        else:
            pieces.append(char)
        index += 1
    return ''.join(pieces)


def _tokenize(command):
    """Shell tokens with unquoted operators separated and quoted text kept intact."""
    lexer = shlex.shlex(_marked(command or ''), posix=True, punctuation_chars='();<>|&`')
    lexer.whitespace_split = True
    lexer.commenters = '#'
    try:
        raw = list(lexer)
    except ValueError:
        return []
    marks = [_QUOTED, _QUOTE_END, *_ESCAPE_VALUES]
    tokens = []
    for token in raw:
        text = token
        for placeholder, char in _ESCAPE_VALUES.items():
            text = text.replace(placeholder, char)
        word = _Word(text.replace(_QUOTED, '').replace(_QUOTE_END, ''))
        word.quoted = any(mark in token for mark in marks)
        equals = token.find('=')
        # `A='b'` is an assignment, while `'A'=b`, `A"=b"` and `A\=b` are commands.
        word.name_quoted = equals < 0 or any(mark in token[:equals] for mark in marks)
        tokens.append(word)
    return tokens


def _line_commands(line):
    commands, current, skip_target = [], [], False
    for token in _tokenize(line):
        if skip_target:
            skip_target = False
            continue
        if not token.quoted and (token in _OPERATORS
                                 or token and set(token) <= set('();|&`')):
            if current:
                commands.append(current)
            current = []
        elif not token.quoted and re.fullmatch(r'&?\d?[<>]+\|?&?\d?', token):
            if current and current[-1].isdigit():
                current.pop()
            skip_target = True
        elif not token.quoted and re.fullmatch(r'&?\d?[<>]+\|?&?\S+', token):
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
            if char == '\n':
                # The shell removes a backslash-newline continuation entirely.
                current.pop()
            else:
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
_ARITHMETIC = (('$((', '))'), ('$[', ']'), ('((', '))'))


def _scan_line(line, quote=None, closer=None):
    """Heredoc openers on one physical line plus the quote and arithmetic state left."""
    found, index, word_start, substitutions = [], 0, True, 0
    while index < len(line):
        char = line[index]
        if closer:
            end = line.rfind(closer, index)
            if end < 0:
                return found, quote, closer
            index = end + len(closer)
            closer = None
            word_start = False
            continue
        if quote:
            if char == '\\' and quote == '"':
                index += 1
            elif char == quote:
                quote = None
            word_start = False
            index += 1
            continue
        if char == '\\':
            word_start = False
            index += 2
            continue
        if char == '#' and word_start:
            return found, quote, closer
        if char in {'"', "'"}:
            quote = char
            word_start = False
            index += 1
            continue
        if line.startswith('<<', index):
            if line.startswith('<<<', index):
                word_start = False
                index += 3
                continue
            index += 2
            strip_tabs = line.startswith('-', index)
            index += 1 if strip_tabs else 0
            while index < len(line) and line[index] in ' \t':
                index += 1
            escaped = line[index:index + 1] == '\\'
            if escaped:
                index += 1
            closing = line[index] if line[index:index + 1] in {'"', "'"} else None
            if closing:
                end = line.find(closing, index + 1)
                word = line[index + 1:end] if end != -1 else ''
                index = end + 1 if end != -1 else len(line)
            else:
                match = _HEREDOC_WORD.match(line, index)
                word, index = (match[0], match.end()) if match else ('', len(line))
            if not word:
                return found, quote, closer
            if index < len(line) and line[index] not in ' \t;&|<>()':
                # The delimiter word continues past what the scan can resolve
                # (for example `<<EOF#tag`): keep the body opaque instead of
                # closing it before the shell would.
                return found + [(None, strip_tabs, True)], quote, closer
            found.append((word, strip_tabs, escaped or bool(closing)))
            word_start = False
            continue
        arithmetic = next((pair for pair in _ARITHMETIC if line.startswith(pair[0], index)), None)
        if arithmetic:
            opener, end_token = arithmetic
            end = line.rfind(end_token, index + len(opener))
            if end < 0:
                return found, quote, end_token
            index = end + len(end_token)
            word_start = False
            continue
        if line.startswith('$(', index):
            substitutions += 1
            word_start = False
            index += 2
            continue
        if char == ')':
            if substitutions:
                substitutions -= 1
                word_start = False
            else:
                word_start = True
            index += 1
            continue
        if char in ' \t\n;&|(':
            word_start = True
            index += 1
            continue
        word_start = False
        index += 1
    return found, quote, closer


def _continued_body(raw):
    """An unquoted body line ending in an odd backslash run continues onto the next line."""
    return (len(raw) - len(raw.rstrip('\\'))) % 2 == 1


def _without_heredoc_bodies(command):
    """Command without heredoc bodies, which the shell never runs as commands."""
    kept, pending, quote, closer, continued = [], [], None, None, False
    for raw in (command or '').split('\n'):
        line = raw.rstrip('\r')
        if pending:
            word, strip_tabs, quoted = pending[0]
            if not continued and (line.lstrip('\t') if strip_tabs else line) == word:
                pending.pop(0)
                continued = False
            else:
                continued = not quoted and _continued_body(raw)
            continue
        kept.append(line)
        found, quote, closer = _scan_line(line, quote, closer)
        if any(word is None for word, _, _ in found):
            return ''
        if not closer:
            pending.extend(found)
    return '\n'.join(kept)


def _shell_commands(command):
    """Split a command line into simple commands without breaking quoted arguments."""
    commands = []
    for line in _lines(_without_heredoc_bodies(command)):
        commands.extend(_line_commands(line))
    return commands


def _assignment(tokens):
    """True when the leading word is a NAME=value prefix, not a command name."""
    return bool(tokens) and not tokens[0].name_quoted and bool(_ASSIGNMENT.fullmatch(tokens[0]))


def _nice_arguments(tokens):
    """Arguments after nice's own options; empty when nice cannot run a command."""
    while tokens:
        option = tokens[0]
        if option in {'-n', '--adjustment'}:
            if len(tokens) < 2 or not _INTEGER.fullmatch(tokens[1]):
                return []
            tokens = tokens[2:]
        elif option.startswith('--adjustment='):
            if not _INTEGER.fullmatch(option.split('=', 1)[1]):
                return []
            tokens = tokens[1:]
        elif option.startswith('-n') and _INTEGER.fullmatch(option[2:]):
            tokens = tokens[1:]
        elif re.fullmatch(r'--?[+-]?\d+', option):
            tokens = tokens[1:]
        elif option == '--':
            return tokens[1:]
        else:
            break
    return tokens


def _timeout_arguments(tokens):
    """Arguments after timeout's own options; empty when timeout cannot run a command."""
    while tokens and tokens[0].startswith('-'):
        option = tokens[0]
        if option == '--':
            tokens = tokens[1:]
            break
        if option.startswith('--'):
            name, _, value = option.partition('=')
            if name in {'--signal', '--kill-after'}:
                if value:
                    tokens = tokens[1:]
                elif len(tokens) > 1:
                    tokens = tokens[2:]
                else:
                    return []
            elif option in {'--verbose', '--foreground', '--preserve-status'}:
                tokens = tokens[1:]
            else:
                return []
            continue
        letters, consumed = option[1:], 1
        while letters:
            letter, letters = letters[0], letters[1:]
            if letter in 'vfp':
                continue
            if letter not in 'sk':
                return []
            if not letters:
                if len(tokens) < 2:
                    return []
                consumed = 2
            letters = ''
        tokens = tokens[consumed:]
    if not tokens or not _DURATION.fullmatch(tokens[0]):
        return []
    return tokens[1:]


def _wrapper_arguments(name, tokens, external=False):
    """Arguments after a wrapper's own options; empty when it cannot run a command."""
    flags, operands, skipped = (_GNU_TIME_OPTIONS if name == 'time' and external
                                else _WRAPPER_OPTIONS[name])
    while tokens and tokens[0].startswith('-'):
        option = tokens[0]
        if name == 'time' and not external and option.quoted:
            break             # the keyword reads only unquoted `-p`/`--` as options
        if option == '--':
            return tokens[1:]
        if option == '-':
            if name == 'env':      # `env -` is `env -i`, not a command named `-`.
                tokens = tokens[1:]
                continue
            break
        if option in skipped:
            return []
        if option in flags:
            tokens = tokens[1:]
            continue
        head, separator, _ = option.partition('=')
        if head in operands:
            if separator:
                tokens = tokens[1:]
            elif len(tokens) > operands[head]:
                tokens = tokens[1 + operands[head]:]
            else:
                return []
            continue
        letters = option[1:]
        if option.startswith('--') or len(letters) < 2:
            return []
        if any('-' + letter in skipped for letter in letters):
            return []
        if all('-' + letter in flags for letter in letters):
            tokens = tokens[1:]
        elif '-' + letters[0] in operands:
            if len(letters) > 1:
                tokens = tokens[1:]
            elif len(tokens) > operands['-' + letters[0]]:
                tokens = tokens[1 + operands['-' + letters[0]]:]
            else:
                return []
        else:
            return []
    return tokens


def _head_read(tokens):
    while tokens and tokens[0] in _RESERVED and not tokens[0].quoted:
        tokens = tokens[1:]
    # `A=1 timeout 5 git ...` assigns for the wrapper, so strip prefixes first.
    while _assignment(tokens):
        tokens = tokens[1:]
    shell = True
    while tokens:
        name = os.path.basename(tokens[0])
        if name in _SHELLS:
            if len(tokens) > 2 and re.fullmatch(r'-[A-Za-z]*c', tokens[1]):
                return _rev_parse_head(tokens[2])
            return False
        if name not in _WRAPPERS:
            break
        if not shell and name in {'command', 'exec', 'time'}:
            return False
        external = '/' in tokens[0]
        tokens = tokens[1:]
        if name == 'nice':
            tokens = _nice_arguments(tokens)
        elif name == 'timeout':
            tokens = _timeout_arguments(tokens)
        else:
            tokens = _wrapper_arguments(name, tokens, external)
        if name in _ASSIGNING_WRAPPERS:
            # env and sudo re-parse their arguments, so quoting does not hide a prefix.
            while tokens and _ASSIGNMENT.fullmatch(tokens[0]):
                tokens = tokens[1:]
        elif name == 'time' and not external:
            while _assignment(tokens):
                tokens = tokens[1:]
        shell = name == 'command' or name == 'time' and not external
    if not tokens or os.path.basename(tokens[0]) != 'git':
        return False
    rest = tokens[1:]
    while rest:
        head, separator, _ = rest[0].partition('=')
        if rest[0] in _GIT_GLOBAL_OPTIONS and len(rest) > 1:
            rest = rest[2:]
        elif separator and head in _GIT_GLOBAL_OPTIONS:
            rest = rest[1:]
        elif rest[0] in _GIT_GLOBAL_FLAGS:
            rest = rest[1:]
        else:
            break
    if not rest or rest[0] != 'rev-parse':
        return False
    arguments = rest[1:]
    if '--' in arguments and any(word in _HEAD_WORDS
                                 for word in arguments[arguments.index('--'):]):
        return False
    revisions = [a for a in arguments if not a.startswith('-')]
    flags = {a for a in arguments if a.startswith('-')}
    return bool(revisions) and all(r in _HEAD_WORDS for r in revisions) and flags <= _REV_PARSE_FLAGS


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
