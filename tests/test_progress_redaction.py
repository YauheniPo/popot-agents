import time
import unittest

from popot_agents.progress import redact_text


REDACTED = '[REDACTED]'


class RedactTextTests(unittest.TestCase):
    """Regression tests for the credential regexes in redact_text.

    Covers code-scanning alerts #17/#18 (py/polynomial-redos) and the
    SonarQube python:S5843 simplification of the same patterns.
    """

    def test_private_key_block_is_redacted(self):
        text = ('before\n-----BEGIN RSA PRIVATE KEY-----\nabc\ndef\n'
                '-----END RSA PRIVATE KEY-----\nafter')
        self.assertEqual(redact_text(text), f'before\n{REDACTED}\nafter')

    def test_unterminated_private_key_marker_does_not_backtrack_quadratically(self):
        # Alert #17 trigger: a BEGIN marker without a matching END marker.
        text = '-----BEGIN PRIVATE KEY-----' + 'A' * 100_000
        start = time.monotonic()
        result = redact_text(text)
        elapsed = time.monotonic() - start
        self.assertIn('-----BEGIN PRIVATE KEY-----', result)
        self.assertLess(elapsed, 2.0)

    def test_malformed_url_credentials_input_is_safe_and_fast(self):
        # Alert #18 trigger: 'http://!:' style malformed authority.
        text = 'http://!:' + ':' * 100_000
        start = time.monotonic()
        result = redact_text(text)
        elapsed = time.monotonic() - start
        self.assertEqual(result, text)
        self.assertLess(elapsed, 2.0)

    def test_url_userinfo_password_is_redacted(self):
        self.assertEqual(
            redact_text('clone https://user:hunter2@example.com/repo.git'),
            f'clone https://{REDACTED}@example.com/repo.git')

    def test_github_tokens_are_redacted(self):
        classic = 'ghp_' + 'a' * 36
        fine_grained = 'github_pat_' + 'b' * 40
        self.assertEqual(redact_text(f'token {classic} end'), f'token {REDACTED} end')
        self.assertEqual(redact_text(f'token {fine_grained} end'), f'token {REDACTED} end')

    def test_openai_style_key_is_redacted(self):
        key = 'sk-' + 'c' * 40
        self.assertEqual(redact_text(f'key {key} end'), f'key {REDACTED} end')

    def test_password_assignment_keeps_label_and_redacts_value(self):
        self.assertEqual(redact_text('password: hunter2'), f'password: {REDACTED}')
        self.assertEqual(redact_text('Пароль=sekret'), f'Пароль={REDACTED}')

    def test_bearer_header_is_redacted(self):
        self.assertEqual(redact_text('Authorization: Bearer abc123'),
                         f'Authorization: Bearer {REDACTED}')

    def test_plain_text_is_unchanged(self):
        text = 'ordinary progress update without secrets'
        self.assertEqual(redact_text(text), text)


if __name__ == '__main__':
    unittest.main()
