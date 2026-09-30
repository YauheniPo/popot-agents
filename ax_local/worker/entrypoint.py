"""Prepare AX's durable workspace for the unprivileged tool user, then serve chat."""

import os

from popot_agents.worker.session_worker import serve


def main() -> None:
    os.makedirs("/workspace", exist_ok=True)
    if os.geteuid() == 0:
        # AX's gVisor workspace mount denies chown even to container root.
        # This workspace belongs to one actor; the sticky bit lets UID 10001
        # create files without letting it remove files owned by root.
        os.chmod("/workspace", 0o1777)
    serve()


if __name__ == "__main__":
    main()
