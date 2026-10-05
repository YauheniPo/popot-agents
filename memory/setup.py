"""Create a private local Cognee password without printing credentials."""

import os
from pathlib import Path
import re
import secrets


def configure(path: Path) -> None:
    content = path.read_text() if path.exists() else ''
    found = re.search(r'^COGNEE_PASSWORD=(.*)$', content, re.MULTILINE)
    if not found or not found.group(1).strip().strip('"\''):
        entry = 'COGNEE_PASSWORD=' + secrets.token_urlsafe(32)
        if found:
            content = content[:found.start()] + entry + content[found.end():]
        else:
            content = content.rstrip('\n') + '\n' + entry + '\n'
        # O_CREAT's mode protects a newly created file before writing the secret.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(content)
    path.chmod(0o600)


if __name__ == '__main__':
    configure(Path(__file__).resolve().parents[1] / '.env')
