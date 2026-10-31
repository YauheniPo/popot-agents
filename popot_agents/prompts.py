"""Read versioned Markdown prompts with strict, non-recursive substitutions."""
import re
from pathlib import Path


PROMPTS_ROOT = Path(__file__).resolve().parents[1] / 'config' / 'prompts'
PLACEHOLDER = re.compile(r'\{\{([a-z][a-z0-9_]*)\}\}')


def render_prompt(name: str, **values) -> str:
    if not re.fullmatch(r'[a-z][a-z0-9_]*(?:/[a-z][a-z0-9_]*)*', name):
        raise ValueError(f'Invalid prompt name: {name}')
    try:
        text = (PROMPTS_ROOT / (name + '.md')).read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f'Cannot read prompt: {name}') from exc
    if not text:
        raise ValueError(f'Empty prompt: {name}')
    expected = set(PLACEHOLDER.findall(text))
    if expected != set(values):
        raise ValueError(f'Prompt {name}: missing {sorted(expected - set(values))}; '
                         f'extra {sorted(set(values) - expected)}')
    return PLACEHOLDER.sub(lambda match: str(values[match[1]]), text)
