"""Prepare AX's durable workspace for the unprivileged tool user, then serve chat."""

import os
import socket
import stat

from popot_agents.worker.session_worker import SOCKET_PATH, serve
from .delegation import make_tools
from .process_metrics import ProcessMetrics


def prepare_socket(path: str) -> None:
    """Discard a dead listener left in a DATA snapshot, preserving other files."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise RuntimeError('AX chat socket path is not a socket')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        try:
            probe.connect(path)
        except ConnectionRefusedError:
            os.unlink(path)
        else:
            raise RuntimeError('AX chat socket is already in use')


def main() -> None:
    os.makedirs("/workspace", mode=0o700, exist_ok=True)
    if os.geteuid() == 0:
        # Substrate creates the private overlay root as 0700. UID 10001 needs
        # search permission on / to reach Python and its stdlib after the drop.
        # Preserve existing read/write permissions; add only directory search.
        root_mode = os.stat("/").st_mode & 0o7777
        if root_mode & 0o111 != 0o111:
            os.chmod("/", root_mode | 0o111)
        # Pin the mount rather than following a replacement path or symlink.
        descriptor = os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            tool_user = os.getenv("POPOT_TOOLS_UID") == "10001"
            os.fchmod(descriptor, 0o700)
            if tool_user:
                os.fchown(descriptor, 10001, 10001)
        finally:
            os.close(descriptor)
    prepare_socket(SOCKET_PATH)
    tools = make_tools()
    with ProcessMetrics.from_env():
        if tools:
            serve(extra_tools=tools)
        else:
            serve()


if __name__ == "__main__":
    main()
