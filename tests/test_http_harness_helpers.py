from popot_agents.worker.http_harness import _mentions_github_url


def test_pull_request_link_counts_as_a_github_link():
    messages = [{'role': 'user', 'content': 'Please review https://github.com/YauheniPo/popot-agents/pull/37'}]
    assert _mentions_github_url(messages)


def test_bracketed_link_counts_as_a_github_link():
    messages = [{'role': 'assistant', 'content': 'see repo'},
                {'role': 'user', 'content': '(https://github.com/YauheniPo/popot-agents), thanks'}]
    assert _mentions_github_url(messages)


def test_lookalike_hosts_are_not_github_links():
    for content in ('https://evil.example/github.com/pull/1',
                    'https://github.com.evil.example/YauheniPo/popot-agents',
                    'https://evil.example/?next=https://github.com/YauheniPo'):
        assert not _mentions_github_url([{'role': 'user', 'content': content}]), content


def test_plain_github_mentions_are_not_links():
    assert not _mentions_github_url([{'role': 'user', 'content': 'see github.com/YauheniPo/popot-agents'}])


def test_markdown_wrapped_links_count_as_github_links():
    for content in ('[pull/37](https://github.com/YauheniPo/popot-agents/pull/37)',
                    '**https://github.com/YauheniPo/popot-agents**',
                    'see <https://github.com/YauheniPo/popot-agents/pull/37>.'):
        assert _mentions_github_url([{'role': 'user', 'content': content}]), content


def test_bracketed_hosts_are_skipped_instead_of_raised():
    for content in ('[ax](https://[::1])', 'https://[::1]', 'see [a](https://[::1/pull/37)'):
        assert not _mentions_github_url([{'role': 'user', 'content': content}]), content
    assert _mentions_github_url([{'role': 'user', 'content': '[ax](https://[::1]) https://github.com/YauheniPo/popot-agents/pull/37'}])


def test_clear_text_github_links_do_not_count_as_verification():
    assert not _mentions_github_url([{'role': 'user', 'content': 'see http://github.com/YauheniPo/popot-agents/pull/37'}])
