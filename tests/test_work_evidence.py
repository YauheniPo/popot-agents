import json
import os
import subprocess
import tempfile
import unittest

from popot_agents.tools import TOOL_SCHEMAS


class WorkEvidenceTests(unittest.TestCase):
    def test_node_skip_names_and_reasons_survive_receipt_transport(self):
        from popot_agents.work_evidence import observe_tool_result, evidence_event
        cases = (
            'ok 1 - needs upstream # SKIP WORKSPACE_UPSTREAM_DIR missing\n'
            'ok 2 - placeholder # SKIP\n# pass 1\n# skipped 2\n',
            '﹣ needs upstream (0.1ms) # WORKSPACE_UPSTREAM_DIR missing\n'
            '﹣ placeholder (0.2ms) # SKIP\nℹ pass 1\nℹ skipped 2\n')
        for output in cases:
            with self.subTest(output=output):
                receipt = observe_tool_result('bash', {'command': 'node --test fixture.mjs'},
                    'exit_code=0\n' + output, 'node-skips')
                self.assertEqual(receipt['skipCount'], 2)
                self.assertEqual(receipt['unknownSkipReasons'], 1)
                self.assertEqual(receipt['skippedTests'], [
                    {'name': 'needs upstream', 'reason': 'WORKSPACE_UPSTREAM_DIR missing'},
                    {'name': 'placeholder', 'reason': None}])
                restored = evidence_event(json.dumps({'event': 'work_evidence', 'receipt': receipt}))
                self.assertEqual(restored['skippedTests'], receipt['skippedTests'])
                self.assertEqual(restored['unknownSkipReasons'], 1)
                from popot_agents.worker.final_result import retained_receipts
                self.assertIn('WORKSPACE_UPSTREAM_DIR missing',
                    ' '.join(retained_receipts([], {}, [receipt])))

    def test_node_skip_summary_without_details_remains_unknown_and_is_bounded(self):
        from popot_agents.work_evidence import observe_tool_result, evidence_event
        receipt = observe_tool_result('bash', {'command': 'node --test fixture.mjs | tail -8'},
            'exit_code=0\n# pass 2\n# skipped 8\n', 'summary')
        self.assertEqual(receipt['skipCount'], 8)
        self.assertEqual(receipt['unknownSkipReasons'], 8)
        self.assertEqual(receipt['skippedTests'], [])
        detail = ''.join(f'ok {i+1} - case {i} # SKIP prerequisite\n' for i in range(20))
        receipt = observe_tool_result('bash', {'command': 'node --test fixture.mjs'},
            'exit_code=0\n' + detail + '# pass 1\n# skipped 20\n', 'bounded')
        self.assertEqual(len(receipt['skippedTests']), 16)
        self.assertEqual(receipt['unknownSkipReasons'], 0)
        self.assertTrue(receipt['skipsTruncated'])
        receipt['skippedTests'][0]['reason'] = 'x' * 1000
        restored = evidence_event(json.dumps({'event': 'work_evidence', 'receipt': receipt}))
        self.assertLessEqual(len(restored['skippedTests'][0]['reason']), 320)

    def test_loaded_github_file_observes_commit_revision_separately_from_blob_and_pr_head(self):
        from popot_agents.work_evidence import observe_tool_result, evidence_event
        from popot_agents.worker.verification import observed_revision, acceptance_revision
        sha, blob = 'a' * 40, 'b' * 40
        args = {'owner': 'example', 'repo': 'repo', 'ref': sha, 'path': 'src/app.py'}
        output = (f'successfully downloaded text file (SHA: {blob})\n'
                  f'[repo://example/repo/sha/{sha}/contents/src/app.py]\nprint("hello")\n')
        receipt = observe_tool_result('mcp__github__get_file_contents', args, output, 'file')
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt['revision'], sha)
        self.assertEqual(receipt['revisionSource'], 'repository_content')
        self.assertEqual(receipt['repository'], 'example/repo')
        restored = evidence_event(json.dumps({'event': 'work_evidence', 'receipt': receipt}))
        self.assertEqual(restored['revisionSource'], 'repository_content')
        self.assertEqual(observed_revision({'revision': sha}, [restored]), sha)
        self.assertIsNone(acceptance_revision([], [restored]))
        for name, arguments, text in (
                ('read_file', args, output),
                ('mcp__github__get_file_contents', {**args, 'ref': blob}, output),
                ('mcp__github__get_file_contents', {**args, 'repo': 'other'}, output),
                ('mcp__github__get_file_contents', {**args, 'path': 'other.py'}, output),
                ('mcp__github__get_file_contents', args, 'error: ' + output),
                ('mcp__github__get_file_contents', args, 'File body:\n' + output),
                ('mcp__github__get_file_contents', args, output.replace('/sha/' + sha, '/heads/main')),
                ('mcp__github__get_file_contents', args, f'Resource link (content not loaded): repo://example/repo/sha/{sha}/contents/src/app.py')):
            with self.subTest(name=name, arguments=arguments, text=text):
                self.assertNotIn('revision', observe_tool_result(name, arguments, text, 'unverified') or {})

    def test_labeled_head_from_the_same_check_command_is_reusable(self):
        from popot_agents.work_evidence import observe_tool_result, reusable_checks
        sha = 'a' * 40
        command = ('git rev-parse HEAD > /tmp/rev.txt && python3 -m pytest -q > /tmp/run.log; '
                   'echo "PYTEST_EXIT=$?"; echo "REV=$(cat /tmp/rev.txt)"; tail /tmp/run.log')
        receipt = observe_tool_result('bash', {'command': command},
            f'exit_code=0\nPYTEST_EXIT=0\nREV={sha}\n921 passed, 32 skipped\n', 'suite')
        self.assertEqual(receipt.get('revision'), sha)
        self.assertEqual(reusable_checks([receipt], sha), [receipt])

    def test_reading_pytest_summary_does_not_create_another_check_execution(self):
        from popot_agents.work_evidence import observe_tool_result
        command = 'echo "=== pytest summary line ==="; grep passed research/pytest_run.log'
        self.assertIsNone(observe_tool_result('bash', {'command': command},
            'exit_code=0\n=== pytest summary line ===\n921 passed, 32 skipped\n', 'read'))

    def test_head_substitution_is_observed_but_dirty_or_ambiguous_checks_are_not_reused(self):
        from popot_agents.work_evidence import observe_tool_result, reusable_checks
        sha = 'a' * 40
        command = 'echo "HEAD=$(git rev-parse HEAD)"; python3 -m pytest -q'
        clean = observe_tool_result('bash', {'command': command},
            f'exit_code=0\nHEAD={sha}\nDIRTY=0\n3 passed\n', 'clean')
        self.assertEqual(clean.get('revision'), sha)
        dirty = observe_tool_result('bash', {'command': command},
            f'exit_code=0\nHEAD={sha}\nDIRTY=1\n3 passed\n', 'dirty')
        self.assertEqual(reusable_checks([dirty], sha), [])
        mixed = observe_tool_result('bash', {'command': command},
            f'exit_code=0\nHEAD={sha}\nREV={"b" * 40}\n3 passed\n', 'mixed')
        self.assertEqual(reusable_checks([mixed], sha), [])

    def test_dirty_marker_survives_durable_receipt_transport(self):
        from popot_agents.work_evidence import observe_tool_result, evidence_event, reusable_checks
        sha = 'a' * 40
        receipt = observe_tool_result('bash', {'command': 'git rev-parse HEAD; pytest -q'},
            f'exit_code=0\n{sha}\nDIRTY=1\n3 passed\n', 'dirty')
        restored = evidence_event(json.dumps({'event': 'work_evidence', 'receipt': receipt}))
        self.assertEqual(reusable_checks([restored], sha), [])

    def test_explicit_failed_check_exit_and_node_spec_output_are_interpreted(self):
        from popot_agents.work_evidence import observe_tool_result
        failed = observe_tool_result('bash', {'command': 'pytest -q; echo PYTEST_EXIT=$?'},
            'exit_code=0\nPYTEST_EXIT=1\n3 passed\n', 'failed')
        self.assertEqual(failed['status'], 'failed')
        node = observe_tool_result('bash', {'command': 'node --test tests/*.mjs'},
            'exit_code=0\nℹ pass 26\nℹ fail 0\n', 'node')
        self.assertEqual(node['kind'], 'checks')
        self.assertEqual(node['status'], 'passed')

    def test_node_failure_or_cancellation_cannot_be_masked_by_later_shell_success(self):
        from popot_agents.work_evidence import observe_tool_result
        for marker in ('# fail 1', 'ℹ fail 1', '# cancelled 1', 'ℹ cancelled 1'):
            with self.subTest(marker=marker):
                receipt = observe_tool_result('bash', {'command': 'node --test; echo done'},
                    'exit_code=0\n# pass 26\n' + marker + '\n', 'masked-node')
                self.assertEqual(receipt['status'], 'failed')

    def test_interpreter_script_arguments_are_not_test_execution(self):
        from popot_agents.work_evidence import observe_tool_result
        for command in ('python3 -c \'print("921 passed")\' -m pytest',
                        'python3 read_log.py -m pytest', 'node read_log.js --test',
                        'node -e \'console.log("921 passed")\' --test'):
            with self.subTest(command=command):
                self.assertIsNone(observe_tool_result('bash', {'command': command},
                    'exit_code=0\n921 passed\n', 'script-args'))

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

    def test_clone_revision_requires_the_native_success_receipt_for_the_requested_commit(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'd' * 40
        arguments = {'url': 'https://example.com/repo.git', 'directory': 'checkout', 'commit_sha': sha}
        output = f'cloned to /workspace/checkout\nverified_commit={sha}\nexit_code=0\n{sha}\n'
        for name, args, result in (
                ('git_clone', {k: v for k, v in arguments.items() if k != 'commit_sha'}, output),
                ('git_clone', {**arguments, 'commit_sha': 'e' * 40}, output),
                ('git_clone', arguments, output.replace('exit_code=0', 'exit_code=1')),
                ('git_clone', arguments, output.replace(f'\n{sha}\n', '\n' + 'e' * 40 + '\n')),
                ('git_clone', arguments, 'git_clone failed: ' + output),
                ('git_clone', arguments, f'verified_commit={sha}'),
                ('read_file', {'path': 'old-output.txt'}, output)):
            with self.subTest(name=name, args=args, result=result):
                receipt = observe_tool_result(name, args, result, 'clone')
                self.assertNotIn('revision', receipt or {})

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

    def test_wrapper_option_operands_and_assignment_prefixes_match_bash(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'db' * 20
        # Options that name an operand or print a description instead of running
        # the command leave the word after them as the command, so git never runs.
        for command in ('command -v git rev-parse HEAD',
                        'command -V git rev-parse HEAD',
                        'command -pv git rev-parse HEAD',
                        'sudo -u git rev-parse HEAD',
                        'sudo -p git rev-parse HEAD',
                        'sudo --preserve-env=GIT_DIR rev-parse HEAD',
                        'env -u git rev-parse HEAD',
                        'env --unset=git rev-parse HEAD',
                        'exec -a git rev-parse HEAD',
                        'time -f git rev-parse HEAD',
                        'nohup --help git rev-parse HEAD',
                        'nice A=b git rev-parse HEAD',
                        'nohup A=b git rev-parse HEAD',
                        'timeout 5 A=b git rev-parse HEAD'):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'not-run'), command)
        # The same grammar keeps real read-backs observable, including assignment
        # prefixes that env, sudo and the time keyword really do apply.
        for command in ('command -- git rev-parse HEAD',
                        'command -p git rev-parse HEAD',
                        'sudo -u deploy git rev-parse HEAD',
                        'sudo -E A=b git rev-parse HEAD',
                        'env -u GIT_DIR git rev-parse HEAD',
                        'env -i A=b git rev-parse HEAD',
                        'env - git rev-parse HEAD',
                        'env - A=b git rev-parse HEAD',
                        'env --chdir=/tmp git rev-parse HEAD',
                        "env '-u' GIT_DIR git rev-parse HEAD",
                        'exec -a mygit git rev-parse HEAD',
                        'sudo --preserve-env=GIT_DIR git rev-parse HEAD',
                        'sudo --user=deploy git rev-parse HEAD',
                        'sudo --command-timeout=5s git rev-parse HEAD',
                        'time A=b git rev-parse HEAD',
                        'time -p git rev-parse HEAD',
                        'nohup git rev-parse HEAD'):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'runs')
            self.assertEqual(receipt and receipt['revision'], sha, command)

    def test_double_dash_option_end_and_git_global_grammar(self):
        from popot_agents.work_evidence import observe_tool_result
        sha = 'dc' * 20
        # `--` ends option parsing, so a dash word after it is the command name,
        # env rejects `-0` with a command, a shell builtin cannot follow an
        # exec'd wrapper, and unknown git global options abort git.
        for command in ('env -- -i git rev-parse HEAD',
                        'env -- -u GIT_DIR git rev-parse HEAD',
                        'command -- -p git rev-parse HEAD',
                        'command -p -- -p git rev-parse HEAD',
                        'exec -- -c git rev-parse HEAD',
                        'exec -- -a custom git rev-parse HEAD',
                        'env -0i git rev-parse HEAD',
                        'env -0 git rev-parse HEAD',
                        'env command git rev-parse HEAD',
                        'nice command git rev-parse HEAD',
                        'exec command git rev-parse HEAD',
                        'git --bogus=1 rev-parse HEAD',
                        'git --git-dirx=.git rev-parse HEAD',
                        'git --no-pager=true rev-parse HEAD',
                        'git rev-parse -- HEAD',
                        "time '--' git rev-parse HEAD",
                        'time \\-- git rev-parse HEAD',
                        "time '-p' '--' git rev-parse HEAD",
                        "time '-p' -- git rev-parse HEAD",
                        "time '-p' git rev-parse HEAD",
                        "time '-p' A=1 git rev-parse HEAD"):
            self.assertIsNone(observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'not-run'), command)
        # The same spellings still count when bash really runs the read-back.
        for command in ('env -- git rev-parse HEAD',
                        'env - git rev-parse HEAD',
                        'env - A=b git rev-parse HEAD',
                        'command -- git rev-parse HEAD',
                        'nohup -- git rev-parse HEAD',
                        'time -- git rev-parse HEAD',
                        'time -p -- git rev-parse HEAD',
                        'git rev-parse HEAD --',
                        'git --git-dir=.git rev-parse HEAD',
                        'exec env git rev-parse HEAD',
                        "env bash -c 'git rev-parse HEAD'"):
            receipt = observe_tool_result('bash', {'command': command},
                f'exit_code=0\n{sha}\n', 'runs')
            self.assertEqual(receipt and receipt['revision'], sha, command)
