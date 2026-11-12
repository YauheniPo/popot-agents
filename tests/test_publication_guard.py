"""Publication ownership applies to commands, not quoted source or search text."""

import os
import shutil
import subprocess

import pytest

from popot_agents.tools import ToolInputError
from popot_agents.worker.delegation_policy import check_publication_owner


def guard(command, role='qa_engineer', owner='implementation'):
    check_publication_owner('bash', {'command': command}, {
        'role': role, 'session_id': 'verification', 'publication_owner': owner})


@pytest.mark.parametrize('command', [
    "printf '%s\\n' 'git push origin main'",
    "rg 'git push' tests",
    "rg 'git commit' tests",
    "python3 -c \"print('git push origin main')\"",
    "python3 - <<'PY'\nprint('git push origin main')\nprint('git commit')\nPY",
    "cat <<'EOF'\ngit push origin main\nEOF\ngit status",
    "git log --grep='git push'",
    "git log --grep commit",
    "git show commit",
    "git diff; # git push origin main\ngit status",
    "printf '%s\\n' 'first line\ngit push origin main'",
    "bash -c \"printf '%s' 'git push origin main'\"",
    "command -v git push",
    "env --null git push",
    "printf '%s' '$(git push origin main)'",
    "printf '%s' '`git push origin main`'",
    'printf "%s" "\\$(git push origin main)"',
    '# echo "$(git push origin main)"\ngit status',
])
def test_literal_commands_and_read_only_operations_are_allowed(command):
    guard(command, role='code_reviewer')


@pytest.mark.parametrize('prefix', [
    'git',
    'git --no-pager',
    'git -c a=b --no-pager',
    'git --work-tree=/w',
    'git --git-dir=/g',
    'git --work-tree /w --git-dir /g',
    'git -C "a directory"',
    'git -Ca-directory -ca=b',
    'git --no-replace-objects --literal-pathspecs',
    '/usr/bin/git -P',
])
def test_git_global_options_do_not_bypass_reviewer_or_publication_owner(prefix):
    with pytest.raises(ToolInputError, match='reviewer'):
        guard(prefix + ' commit -m fix', role='code_reviewer')
    with pytest.raises(ToolInputError, match='reviewer'):
        guard(prefix + ' commit-tree HEAD^{tree}', role='code_reviewer')
    with pytest.raises(ToolInputError, match='integration owner'):
        guard(prefix + ' push origin feature')


@pytest.mark.parametrize('command', [
    'git status; git --no-pager push origin feature',
    'git status && git --no-pager push origin feature',
    'git status || git --no-pager push origin feature',
    'git --no-pager push origin feature | cat',
    'git status\ngit --no-pager push origin feature',
    'git status & git --no-pager push origin feature',
    "bash -lc 'git --no-pager push origin feature'",
    "sh -c 'git --no-pager push origin feature'",
    'command git --no-pager push origin feature',
    'env A=1 git --no-pager push origin feature',
    'A=1 git --no-pager push origin feature',
    'timeout 5 git --no-pager push origin feature',
    "git status\ncat <<'EOF'\nexample\nEOF\ngit --no-pager push origin feature",
    'gh pr create --title fix',
    'gh --repo owner/repo pr merge 47',
    'gh -R owner/repo pr create --title fix',
    'printf "%s" "$(git --no-pager push origin feature)"',
    'echo $(git --no-pager push origin feature)',
    'printf "%s" "`git --no-pager push origin feature`"',
    'printf "%s" "$(printf \'%s\' \')\'; git --no-pager push origin feature)"',
])
def test_non_owner_cannot_publish_through_segments_or_wrappers(command):
    with pytest.raises(ToolInputError, match='integration owner'):
        guard(command)


def test_owner_can_publish_but_reviewer_cannot_commit_even_as_owner():
    guard('git --no-pager push origin feature', owner='verification')
    with pytest.raises(ToolInputError, match='reviewer'):
        guard('git --no-pager commit -m fix', role='code_reviewer', owner='verification')


def test_non_owner_can_prepare_local_commits_and_publish_reviews():
    guard('git --no-pager commit -m fix')
    check_publication_owner('mcp__github__pull_request_review_write', {}, {
        'role': 'code_reviewer', 'session_id': 'verification',
        'publication_owner': 'implementation'})


@pytest.mark.parametrize(('command', 'invokes_git'), [
    ('cat <<EOF\n$(git push origin feature)\nEOF', True),
    ("cat <<EOF\n'$(git push origin feature)'\nEOF", True),
    ('cat <<EOF\n# `git push origin feature`\nEOF', True),
    ("cat <<'EOF'\n$(git push origin feature)\nEOF", False),
    ('cat <<EOF\n\\$(git push origin feature)\nEOF', False),
    ('cat <(git push origin feature)', True),
    ("printf '%s' '<(git push origin feature)'", False),
    ("bash <<'EOF'\ngit push origin feature\nEOF", True),
    ("bash <<'EOF'\nprintf '%s' 'git push origin feature'\nEOF", False),
    ("printf 'before'; bash <<'EOF'\ngit push origin feature\nEOF", True),
    ("bash -c cat <<'EOF'\ngit push origin feature\nEOF", False),
    ("bash <<'FIRST' <<'LAST'\ngit push origin feature\nFIRST\nprintf safe\nLAST", False),
    ("bash -eu -o pipefail <<'SH'\ngit --no-pager push origin feature\nSH", True),
    ("bash <<'SH' </dev/null\ngit push origin feature\nSH", False),
    ("bash --noprofile -s arg <<'SH'\ngit push origin feature\nSH", True),
    ("bash <<'SH' 2>/dev/null\ngit push origin feature\nSH", True),
    ("bash <<'SH' 0</dev/null\ngit push origin feature\nSH", False),
])
def test_expansions_match_bash_execution_without_running_real_git(tmp_path, command, invokes_git):
    bash = shutil.which('bash')
    if not bash:
        pytest.skip('requires bash for shell-semantics comparison')
    marker = tmp_path / 'calls'
    stub = tmp_path / 'git'
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GUARD_PROBE_LOG"\n')
    stub.chmod(0o700)
    result = subprocess.run([bash, '-c', command], capture_output=True, text=True, timeout=5,
                            env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
                                 'GUARD_PROBE_LOG': str(marker)})
    assert result.returncode == 0, result.stderr
    assert marker.exists() == invokes_git
    if invokes_git:
        with pytest.raises(ToolInputError, match='integration owner'):
            guard(command)
    else:
        guard(command)
