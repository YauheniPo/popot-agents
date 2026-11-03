import json
import os
import subprocess
import tempfile
import unittest

from popot_agents.tools import TOOL_SCHEMAS


class WorkEvidenceTests(unittest.TestCase):
    @unittest.skipUnless(os.getenv('AX_TEST_WORKER_IMAGE'), 'requires a built AX worker image')
    def test_shared_module_is_packaged_in_worker_image(self):
        result = subprocess.run(['docker','run','--rm','--entrypoint','python',
            os.environ['AX_TEST_WORKER_IMAGE'], '-c',
            'from popot_agents.worker.http_harness import run_http; from popot_agents.work_evidence import observe_tool_result'],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_schema_advertises_the_runtime_limit(self):
        schema = TOOL_SCHEMAS['bash']['function']
        self.assertEqual(schema['parameters']['properties']['command'].get('maxLength'), 4000)
        self.assertIn('write_file', schema['description'])

    def test_checks_receipt_requires_success_and_revision_in_same_result(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'a' * 40
        receipt = observe_tool_result('bash', {'command':'git rev-parse HEAD; python3 -m unittest'},
            f'exit_code=0\n{sha}\nRan 7 tests in 1.2s\nOK\n', 'checks')
        self.assertEqual(receipt['kind'], 'checks')
        self.assertEqual(receipt['status'], 'passed')
        self.assertEqual(receipt['revision'], sha)
        self.assertEqual(receipt['source'], 'tool_result')
        failed = observe_tool_result('bash', {'command':'python3 -m unittest'},
            'exit_code=1\nFAILED (failures=1)', 'bad')
        self.assertEqual(failed['status'], 'failed')
        self.assertNotIn('revision', failed)

    def test_observations_are_durable_without_overwriting_outcome(self):
        from popot_agents.work_evidence import observe_tool_result
        from popot_agents.orchestrator.session_store import SessionStore
        from popot_agents.orchestrator.work_status import build_work_status
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            sid = 'a'*16
            config = {'_workflow':{'state':'running','outcome':{'status':'partial',
                'evidence':[], 'remaining_work':['review'], 'errors':[]}}}
            store.save(sid, 'test', [], 'backend_engineer', config)
            receipt = observe_tool_result('bash', {'command':'git push'},
                'exit_code=0\nTo https://github.com/example/repo.git\n abc..def HEAD -> fix\n', 'push')
            store.record_evidence(sid, receipt)
            store.record_evidence(sid, receipt)
            store.save(sid, 'test', [], 'backend_engineer', config)
            saved = SessionStore(directory).get(sid)
            report = build_work_status(saved, [saved])['tasks'][0]
            self.assertEqual(len(report['milestones']), 1)
            self.assertEqual(report['milestones'][0]['kind'], 'publication')
            self.assertEqual(report['outcome']['status'], 'partial')

    def test_review_and_qa_have_distinct_verification_responsibilities(self):
        from popot_agents.prompts import render_prompt
        self.assertIn('full test suite', render_prompt('delegation/reviewer'))
        prompt = render_prompt('workflow/coordinator')
        self.assertIn('exact SHA', prompt)
        self.assertIn('commands', prompt)
        self.assertIn('Do not rerun an unchanged full test suite', render_prompt('delegation/qa'))

    def test_checks_for_other_or_unknown_revision_cannot_be_reused(self):
        from popot_agents.work_evidence import observe_tool_result, reusable_checks
        good = observe_tool_result('bash', {'command':'git rev-parse HEAD; python3 -m unittest'},
            'exit_code=0\n'+'a'*40+'\nRan 7 tests in 1s\nOK', 'check')
        self.assertEqual(reusable_checks([good], 'a'*40), [good])
        self.assertEqual(reusable_checks([good], 'b'*40), [])
        self.assertEqual(reusable_checks([good], None), [])

    def test_cli_readback_and_unknown_write_are_observations(self):
        from popot_agents.work_evidence import observe_tool_result, evidence_event
        receipt = observe_tool_result('bash', {'command':'gh pr view 80 --json headRefOid'},
            'exit_code=0\n'+json.dumps({'headRefOid':'c'*40}), 'head')
        self.assertEqual(receipt['revision'], 'c'*40)
        self.assertEqual(receipt['status'], 'observed')
        error = observe_tool_result('mcp__github__push_files', {},
            json.dumps({'isError':True,'outcome_unknown':True,'error':'network failed'}), 'write')
        self.assertEqual(error['status'], 'unknown')
        self.assertTrue(error['outcomeUnknown'])
        self.assertIsNone(evidence_event(json.dumps({'event':'work_evidence','receipt':{'source':'model'}})))

    def test_ci_receipt_names_the_actual_failed_check(self):
        from popot_agents.work_evidence import observe_tool_result
        receipt = observe_tool_result('bash', {'command':'gh pr view 80 --json statusCheckRollup'},
            'exit_code=0\n'+json.dumps({'statusCheckRollup':[
                {'name':'Sonar','status':'COMPLETED','conclusion':'FAILURE'}]}), 'ci')
        self.assertEqual(receipt['kind'], 'ci')
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('Sonar', receipt['summary'])

    def test_reading_a_log_is_not_a_passed_test_execution(self):
        from popot_agents.work_evidence import observe_tool_result
        self.assertIsNone(observe_tool_result('bash', {'command':'cat old-output.txt'},
            'exit_code=0\n77 passed', 'read'))

    def test_later_echo_cannot_mask_a_failed_test_result(self):
        from popot_agents.work_evidence import observe_tool_result
        for output in ('1 failed, 2 passed\nEXIT_CODE=1', 'Ran 7 tests in 1s\nFAILED (failures=1)\nEXIT_CODE=1'):
            result = observe_tool_result('bash', {'command':'python3 -m pytest; echo EXIT_CODE=$?'},
                'exit_code=0\n'+output, 'masked')
            self.assertEqual(result['status'], 'failed')

    def test_revision_receipt_requires_a_real_head_read_back(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'd' * 40
        for command in ('true # git rev-parse HEAD', 'echo "git rev-parse HEAD"',
                        'git rev-parse HEAD~1', 'git rev-parse HEAD2', 'git rev-parse --short HEAD'):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'fake'), command)
        for command in ('git rev-parse HEAD', 'cd repo && git rev-parse --verify HEAD 2>/dev/null',
                        'git -C /workspace/repo rev-parse HEAD'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'real')
            self.assertEqual(receipt['revision'], sha, command)
            self.assertEqual(receipt['kind'], 'revision')

    def test_pr_head_receipt_binds_the_observed_pull_request(self):
        from popot_agents.work_evidence import observe_tool_result, evidence_event
        sha = 'e' * 40
        payload = json.dumps({'number': 37, 'head': {'sha': sha},
            'html_url': 'https://github.com/YauheniPo/popot-agents/pull/37'})
        receipt = observe_tool_result('mcp__github__get_pull_request',
            {'owner': 'YauheniPo', 'repo': 'popot-agents', 'pullNumber': 37}, payload, 'pr')
        self.assertEqual(receipt['revisionSource'], 'pr_head')
        self.assertEqual(receipt['repository'], 'yauhenipo/popot-agents')
        self.assertEqual(receipt['pullRequest'], 37)
        forwarded = evidence_event(json.dumps({'event': 'work_evidence', 'receipt': receipt}))
        self.assertEqual(forwarded['repository'], 'yauhenipo/popot-agents')
        self.assertEqual(forwarded['pullRequest'], 37)
        bare = observe_tool_result('mcp__github__pull_request_read', {}, json.dumps({
            'number': 12, 'headRefOid': sha,
            'base': {'repo': {'full_name': 'YauheniPo/Other-Repo'}}}), 'bare')
        self.assertEqual(bare['repository'], 'yauhenipo/other-repo')
        self.assertEqual(bare['pullRequest'], 12)

    def test_wrapped_and_quoted_head_read_backs_are_recognized(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'f' * 40
        for command in ("bash -c 'cd /x && git rev-parse HEAD'",
                        "bash -lc 'cd /repo && git rev-parse HEAD'",
                        'sudo git rev-parse HEAD', 'env GIT_DIR=/r git rev-parse HEAD',
                        'rev=$(git rev-parse HEAD); echo "$rev"',
                        'bash -c "git status" && git rev-parse HEAD',
                        'git rev-parse HEAD --', 'git rev-parse HEAD^{commit}'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'wrapped')
            self.assertEqual(receipt and receipt['revision'], sha, command)
        for command in ("echo 'cd /x && git rev-parse HEAD'",
                        'git rev-parse HEAD~1^{commit}'):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'not-head'), command)

    def test_multiline_head_read_backs_are_recognized(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'a1' * 20
        for command in ('git rev-parse HEAD\npytest -q', 'pytest -q\ngit rev-parse HEAD',
                        'cd /workspace/repo\ngit -C . rev-parse HEAD\npwd'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'multiline')
            self.assertEqual(receipt and receipt['revision'], sha, command)
        for command in ("bash -c 'cd /x &&\ngit rev-parse HEAD'",
                        "bash -c 'git rev-parse HEAD &&\npwd'"):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'quoted-multiline')
            self.assertEqual(receipt and receipt['revision'], sha, command)
        self.assertIsNone(observe_tool_result('bash', {'command': 'pytest -q\ntrue # git rev-parse HEAD'},
            f'exit_code=0\n{sha}\n', 'multiline-not-head'))

    def test_escaped_quotes_and_heredocs_keep_head_read_backs_honest(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'b2' * 20
        for command in ('echo "a \\" b"\ngit rev-parse HEAD',
                        'bash -c \'echo "a \\" b"\ngit rev-parse HEAD\'',
                        "cat <<'EOF'\nadjusted\nEOF\ngit rev-parse HEAD",
                        'shift=$((1 << 2))\ngit rev-parse HEAD',
                        'shift=$[1 << 2]\ngit rev-parse HEAD',
                        'value=$((1 +\n2 << 1))\ngit rev-parse HEAD',
                        'true;#<<EOF\ngit rev-parse HEAD',
                        'cat > /tmp/head.sh <<EOF\r\ngit rev-parse HEAD\r\nEOF\r\n'
                        'git rev-parse HEAD',
                        "cat <<'EOF'\ngit rev-parse HEAD\\\nEOF\ngit rev-parse HEAD",
                        'x=$(( ((1)) << 3 ))\ngit rev-parse HEAD',
                        '(( 1 +\n2 << 1 ))\ngit rev-parse HEAD',
                        "bash -c 'echo start\ncat <<EOF\nbody\nEOF\ngit rev-parse HEAD'",
                        "bash -lc 'echo start\ncat <<-EOF\n\tbody\n\tEOF\ngit rev-parse HEAD'",
                        '(true)#<<EOF\ngit rev-parse HEAD',
                        '(echo hi)#<<EOF\ngit rev-parse HEAD'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'escaped-quote')
            self.assertEqual(receipt and receipt['revision'], sha, command)
        for command in ("cat > /tmp/head.sh <<'EOF'\ngit rev-parse HEAD\nEOF",
                        'cat > /tmp/head.sh <<EOF\ngit rev-parse HEAD\nEOF',
                        "cat > /tmp/head.sh <<'EOF' # helper\ngit rev-parse HEAD\nEOF",
                        'echo "git rev-parse HEAD"', 'true # git rev-parse HEAD',
                        'printf "x"; echo $(date +%s)#<<EOF\ngit rev-parse HEAD\nEOF',
                        'echo ${v}#<<EOF\ngit rev-parse HEAD\nEOF',
                        'echo {#<<EOF\ngit rev-parse HEAD\nEOF',
                        'echo hi{#<<EOF\ngit rev-parse HEAD\nEOF',
                        'echo $((1 << 2))#<<EOF\ngit rev-parse HEAD\nEOF',
                        'echo {a,b}#<<EOF\ngit rev-parse HEAD\nEOF'):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'not-head'), command)

    def test_heredoc_backslash_continuations_and_backticks_match_bash(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'c3' * 20
        # An unquoted here-document body continues on a trailing backslash, so the
        # delimiter line and the command after it stay inside the body: bash warns
        # and never runs git, hence no revision read-back.
        for command in ('cat > /tmp/f <<EOF\nfoo\\\nEOF\ngit rev-parse HEAD',
                        'cat > /tmp/f <<EOF\nfoo\\\\\\\nEOF\ngit rev-parse HEAD',
                        'cat > /tmp/f <<-EOF\n\tfoo\\\n\tEOF\ngit rev-parse HEAD'):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'continued'), command)
        # A quoted delimiter, an even backslash run or a CRLF ending keeps the
        # delimiter literal, so bash does reach the command and prints the revision.
        for command in ("cat > /tmp/f <<'EOF'\nfoo\\\nEOF\ngit rev-parse HEAD",
                        'cat > /tmp/f <<\\EOF\nfoo\\\nEOF\ngit rev-parse HEAD',
                        'cat > /tmp/f <<EOF\nfoo\\\\\nEOF\ngit rev-parse HEAD',
                        'cat > /tmp/f <<EOF\r\nfoo\\\r\nEOF\r\ngit rev-parse HEAD',
                        'rev=`git rev-parse HEAD`; echo "$rev"'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'executed')
            self.assertEqual(receipt and receipt['revision'], sha, command)
        # Quoted or escaped backticks are literal text, so nothing runs git.
        for command in ("echo '`git rev-parse HEAD`'", 'echo \\`git rev-parse HEAD\\`'):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'literal-backtick'), command)

    def test_control_flow_and_redirections_keep_read_backs_visible(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'd4' * 20
        # Reserved words, negation, brace groups and `&>`/`>|` redirections leave
        # the command itself intact, so bash really does run git rev-parse HEAD.
        for command in ('for i in 1; do git rev-parse HEAD; done',
                        'while true; do git rev-parse HEAD; break; done',
                        'if git rev-parse HEAD; then echo ok; fi',
                        '! git rev-parse HEAD',
                        '{ git rev-parse HEAD; } > /tmp/rev',
                        'git rev-parse HEAD &>/dev/null',
                        'git rev-parse HEAD &>> /tmp/log',
                        'git rev-parse HEAD >| /tmp/rev',
                        'git rev-parse HEAD HEAD',
                        'nice -n 5 git rev-parse HEAD',
                        'timeout -s KILL 30 git rev-parse HEAD',
                        'git rev-parse \\' + '\n' + 'HEAD',
                        'git rev-parse HEAD \\' + '\n' + '| cat'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'control-flow')
            self.assertEqual(receipt and receipt['revision'], sha, command)
        for command in ('nice 5 echo git rev-parse HEAD', 'timeout 30 true',
                        'git rev-parse HEAD~1 &>/dev/null'):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'not-head'), command)

    def test_heredoc_delimiters_that_continue_past_the_scan_stay_opaque(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'd5' * 20
        # Bash reads `<<EOF#tag` and `<<EOF$x` as one delimiter word, so the body
        # never ends: the read-back below it is body text, not a command.
        for command in ('cat > /tmp/f <<EOF#tag\nbody\nEOF\ngit rev-parse HEAD',
                        'cat > /tmp/f <<EOF$x\nbody\nEOF\ngit rev-parse HEAD',
                        "cat > /tmp/f <<'EOF'#tag\nbody\nEOF\ngit rev-parse HEAD"):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'opaque'), command)
        # A separator after the delimiter keeps the body closeable.
        for command in ("cat > /tmp/f <<'EOF' # helper\nbody\nEOF\ngit rev-parse HEAD",
                        'cat > /tmp/f <<EOF; git rev-parse HEAD\nbody\nEOF',
                        'cat > /tmp/f <<EOF> /tmp/rev\nbody\nEOF\ngit rev-parse HEAD'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'closed')
            self.assertEqual(receipt and receipt['revision'], sha, command)

    def test_wrapper_option_grammar_matches_bash(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'd6' * 20
        # nice and timeout run the command only after their own options and, for
        # timeout, a duration; the read-back below the wrapper is the real command.
        for command in ('nice -n 5 git rev-parse HEAD',
                        'nice -n5 git rev-parse HEAD',
                        'nice -5 git rev-parse HEAD',
                        'nice --adjustment=5 git rev-parse HEAD',
                        'nice -- git rev-parse HEAD',
                        'command nice -n 5 git rev-parse HEAD',
                        'timeout -s KILL 30 git rev-parse HEAD',
                        'timeout -sKILL 30 git rev-parse HEAD',
                        'timeout --signal=TERM 30 git rev-parse --verify HEAD',
                        'timeout -k 5s 30s git rev-parse HEAD',
                        'timeout 0.5s git rev-parse HEAD'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'wrapper')
            self.assertEqual(receipt and receipt['revision'], sha, command)
        # `nice 5` runs the word `5` and `timeout git` rejects its duration, so
        # neither reaches git even though a sha-shaped line follows.
        for command in ('nice 5 git rev-parse HEAD',
                        'timeout git rev-parse HEAD',
                        'timeout 30 true',
                        'nice 5 echo git rev-parse HEAD',
                        'timeout -s 30 true',
                        'nice -n git rev-parse HEAD'):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'not-run'), command)

    def test_quoted_reserved_words_and_assignments_are_commands(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'd7' * 20
        # Quoting or escaping turns a reserved word or an assignment into the
        # command name, so git never runs even though a sha-shaped line follows.
        for command in ("printf '%s\\n' " + sha + "; 'done' git rev-parse HEAD || true",
                        "printf '%s\\n' " + sha + "; \\done git rev-parse HEAD || true",
                        "'then' git rev-parse HEAD",
                        "'A=b' git rev-parse HEAD",
                        "printf '%s\\n' " + sha + "; 'fi git rev-parse HEAD' || true"):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'quoted'), command)
        # Quoting the command name still runs it, and unquoted grammar keeps working.
        for command in ("'git' rev-parse HEAD",
                        'A=b git rev-parse HEAD',
                        "env 'A=b' git rev-parse HEAD",
                        'for i in 1; do git rev-parse HEAD; done',
                        'if git rev-parse HEAD; then echo ok; fi'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'runs')
            self.assertEqual(receipt and receipt['revision'], sha, command)

    def test_assignment_quoting_follows_bash(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'd8' * 20
        # Quoting the value keeps NAME=value an assignment for the command it prefixes.
        for command in ("A='b' git rev-parse HEAD",
                        'A="b" git rev-parse HEAD',
                        "A=b'c' git rev-parse HEAD",
                        "A='' git rev-parse HEAD",
                        "A='b' B=c git rev-parse HEAD",
                        "A='b' command git rev-parse HEAD",
                        'A=1 timeout 5 git rev-parse HEAD'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'assignment')
            self.assertEqual(receipt and receipt['revision'], sha, command)
        # Quoting the name or the `=` makes the whole word the command name instead.
        for command in ("'A'=b git rev-parse HEAD",
                        'A"=b" git rev-parse HEAD',
                        'A\\=b git rev-parse HEAD'):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'not-assignment'), command)

    def test_escaped_punctuation_is_a_command_name(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'd9' * 20
        # Quoting or escaping an operator or a reserved word keeps it a word, so
        # bash looks for a command with that name and never reaches git.
        for command in ('\\! git rev-parse HEAD',
                        '\\{ git rev-parse HEAD',
                        '\\} git rev-parse HEAD',
                        "';' git rev-parse HEAD",
                        "printf '%s\\n' " + sha + "; ';' git rev-parse HEAD"):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'word'), command)

    def test_timeout_short_flags_and_nice_legacy_adjustment(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'da' * 20
        for command in ('timeout -f 30 git rev-parse HEAD',
                        'timeout -p 30 git rev-parse HEAD',
                        'timeout -v 30 git rev-parse HEAD',
                        'timeout -v -f 30 git rev-parse HEAD',
                        'timeout -vs KILL 30 git rev-parse HEAD',
                        'timeout -fk5 30 git rev-parse HEAD',
                        'timeout -fsKILL 30 git rev-parse HEAD',
                        'nice --5 git rev-parse HEAD',
                        'nice --10 git rev-parse HEAD',
                        'nice -n -5 git rev-parse HEAD',
                        'nice -n +5 git rev-parse HEAD'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'wrapper')
            self.assertEqual(receipt and receipt['revision'], sha, command)
        for command in ('timeout -x 30 git rev-parse HEAD',
                        'timeout -f 30 true',
                        'nice +5 git rev-parse HEAD'):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'not-run'), command)
