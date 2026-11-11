"""Branch coverage for the work-evidence shell parser and check summaries."""
import unittest

from popot_agents import work_evidence as evidence


class ScanLineTests(unittest.TestCase):
    """The bounded line scanner resolves each heredoc and substitution shape."""

    def test_here_string_is_not_a_heredoc_body(self):
        self.assertEqual(evidence._scan_line('cat <<< "text"'), ([], None, None))

    def test_delimiter_without_a_word_stops_the_scan(self):
        self.assertEqual(evidence._scan_line('cat <<'), ([], None, None))

    def test_escaped_delimiter_is_marked_unquoted(self):
        found, _, _ = evidence._scan_line(r'cat <<\EOF')
        self.assertEqual(found, [('EOF', False, True)])

    def test_unterminated_group_closer_finishes_the_line(self):
        self.assertEqual(evidence._scan_line('abc', None, ')'), ([], None, ')'))

    def test_numeric_descriptor_prefix_is_not_a_shell_script(self):
        bodies = evidence._heredoc_bodies('cat 3<<EOF\nbody\nEOF\n')
        self.assertEqual(bodies[0]['word'], 'EOF')
        self.assertFalse(bodies[0]['script'])

    def test_quoted_delimiter_keeps_the_body_literal(self):
        bodies = evidence._heredoc_bodies('cat <<"EOF"\n$HOME\nEOF\n')
        self.assertTrue(bodies[0]['quoted'])
        self.assertEqual(bodies[0]['lines'], ['$HOME'])


class SubstitutionScanTests(unittest.TestCase):
    """Static substitution scanning honours escapes and nested parentheses."""

    def test_backslash_skips_the_escaped_closer(self):
        self.assertEqual(evidence._substitution_end(r'a\)b)', 0, ')'), 4)

    def test_nested_parentheses_extend_the_substitution(self):
        self.assertEqual(evidence._substitution_end('a(b)c)', 0, ')'), 5)

    def test_unterminated_substitution_has_no_end(self):
        self.assertIsNone(evidence._substitution_end('a(b', 0, ')'))


class ShellStdinTests(unittest.TestCase):
    """Only a supported shell reading stdin is a script consumer."""

    def test_version_flag_is_not_a_script(self):
        self.assertFalse(evidence._shell_reads_stdin(['bash', '--version']))

    def test_command_flag_is_not_a_script(self):
        self.assertFalse(evidence._shell_reads_stdin(['bash', '-c', 'echo hi']))

    def test_rcfile_option_consumes_its_value(self):
        self.assertTrue(evidence._shell_reads_stdin(['bash', '--rcfile', 'profile']))

    def test_stdin_flag_reads_a_script(self):
        self.assertTrue(evidence._shell_reads_stdin(['bash', '-s']))

    def test_quoted_words_and_operators_end_the_redirection_scan(self):
        self.assertFalse(evidence._stdin_overridden('<<EOF "quoted" | cat'))


class RunsChecksTests(unittest.TestCase):
    """Check commands are recognised across runners and wrappers."""

    def test_version_only_invocation_is_not_a_check(self):
        self.assertFalse(evidence._runs_checks('pytest --version'))

    def test_node_test_runner_is_a_check(self):
        self.assertTrue(evidence._runs_checks('node --test tests/a.mjs'))

    def test_python_module_pytest_is_a_check(self):
        self.assertTrue(evidence._runs_checks('python -m pytest'))
        self.assertTrue(evidence._runs_checks('python3 -u -m unittest discover'))

    def test_python_with_package_runner_flags_is_not_a_check(self):
        self.assertFalse(evidence._runs_checks('python -X dev -m pytest'))

    def test_python_without_a_module_is_not_a_check(self):
        self.assertFalse(evidence._runs_checks('python -c "print(1)"'))

    def test_coverage_run_module_is_a_check(self):
        self.assertTrue(evidence._runs_checks('coverage run --branch -m pytest'))
        self.assertTrue(evidence._runs_checks('python -m coverage run -m pytest'))

    def test_coverage_with_unknown_runner_option_is_not_a_check(self):
        self.assertFalse(evidence._runs_checks('coverage run -x -m pytest'))

    def test_package_test_scripts_are_checks(self):
        self.assertTrue(evidence._runs_checks('npm test'))
        self.assertTrue(evidence._runs_checks('npm run test:unit'))
        self.assertTrue(evidence._runs_checks('pnpm run test'))
        self.assertFalse(evidence._runs_checks('npm run lint'))


class SummariesTests(unittest.TestCase):
    """Check summaries pick the earliest marker and tolerate absent markers."""

    def test_earliest_check_fragment_wins(self):
        checks = evidence._check_summary('5 passed then Ran 2 tests')
        self.assertEqual(checks.start(), 0)

    def test_failed_summary_is_reported(self):
        self.assertIsNotNone(evidence._check_summary('FAILED (errors=1)'))

    def test_missing_check_summary(self):
        self.assertIsNone(evidence._check_summary('nothing to report'))

    def test_pass_markers(self):
        self.assertIsNotNone(evidence._pass_summary('OK'))
        self.assertIsNotNone(evidence._pass_summary('# pass 3'))
        self.assertIsNone(evidence._pass_summary('nothing to report'))


class SkipAnnotationTests(unittest.TestCase):
    """TAP and spec skip lines keep their bounded name and reason."""

    def test_tap_skip_with_reason(self):
        self.assertEqual(evidence._tap_skip('ok 1 - needs upstream # SKIP missing'),
                         {'name': 'needs upstream', 'reason': 'missing'})

    def test_tap_skip_without_reason(self):
        self.assertEqual(evidence._tap_skip('ok 2 - placeholder # SKIP'),
                         {'name': 'placeholder', 'reason': None})

    def test_tap_skip_requires_a_separating_space(self):
        self.assertIsNone(evidence._tap_skip('ok 1 - name# SKIP reason'))

    def test_spec_skip_reasonless_marker(self):
        self.assertEqual(evidence._spec_skip('﹣ placeholder (0.2ms) # SKIP'),
                         {'name': 'placeholder', 'reason': None})

    def test_spec_skip_reason(self):
        self.assertEqual(evidence._spec_skip('﹣ needs upstream (0.1ms) # missing'),
                         {'name': 'needs upstream', 'reason': 'missing'})

    def test_spec_line_without_a_file_suffix_is_ignored(self):
        self.assertIsNone(evidence._spec_skip('﹣ name without parens # x'))

    def test_node_skip_counts_combine_reporter_and_annotations(self):
        skips = evidence._node_skips('# skipped 2\nok 1 - a # SKIP b\n')
        self.assertEqual(skips['skipCount'], 2)
        self.assertEqual(skips['skippedTests'], [{'name': 'a', 'reason': 'b'}])
        self.assertEqual(skips['unknownSkipReasons'], 1)


class PullReferenceTests(unittest.TestCase):
    """Pull request references resolve from data links when arguments omit them."""

    def test_reference_from_html_url(self):
        reference = evidence._pull_reference(
            {'html_url': 'https://github.com/acme/repo/pull/42'}, ())
        self.assertEqual(reference, ('acme/repo', 42))
