"""Drop the MCP subprocess to the unprivileged workspace user."""

import os
import shutil
import sys


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("tool command is required")
    # Resolve PATH while Python can still load its stdlib. In AX, execvpe's
    # lazy import of warnings can fail after switching to the workspace UID.
    executable = shutil.which(sys.argv[1])
    if executable is None:
        raise SystemExit("tool executable was not found on PATH")
    arguments = sys.argv[1:]
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    os.execve(executable, arguments, os.environ)
