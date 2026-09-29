"""Drop the MCP subprocess to the unprivileged workspace user."""

import os
import sys


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("tool command is required")
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
