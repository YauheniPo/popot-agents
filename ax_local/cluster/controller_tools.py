"""Adapt pinned local AX templates to run Popot tools as UID/GID 10001.

AX v0.3.1 has no Task securityContext. Its Substrate templates start as UID 0
without SETUID/SETGID; our tool-enabled image needs those two to drop privileges.
"""

import argparse
import hashlib
import subprocess
from pathlib import Path

AX_COMMIT = "e70162a34037c221fe6fadefd98308c05a4ad8f3"
MARKER = "// BuildActorTemplate constructs a Substrate ActorTemplate"
CONTAINER = '\t\t\tName:    "guest",'
SECURITY = '\t\t\tSecurityContext: popotToolSecurityContext(image, envMap),\n'
HELPER = '''// popotToolSecurityContext enables the existing UID 10001 tool isolation.
// All other AX tasks keep upstream's default capabilities.
func popotToolSecurityContext(image string, env map[string]string) *ateapipb.SecurityContext {
	if env["POPOT_TOOLS_UID"] != "10001" || !strings.HasPrefix(image, "localhost:5001/popot-agent-ax@sha256:") {
		return nil
	}
	return &ateapipb.SecurityContext{
		Capabilities: &ateapipb.Capabilities{Add: []string{"SETUID", "SETGID"}},
	}
}

'''
VERSION = hashlib.sha256((AX_COMMIT + HELPER + SECURITY).encode()).hexdigest()


def patched_source(source: str) -> str:
    if "func popotToolSecurityContext(" in source:
        if source.count(HELPER) != 1 or source.count(SECURITY) != 1:
            raise ValueError("Local AX tool-user patch has unexpected changes; inspect the checkout")
        return source
    if source.count(MARKER) != 1 or source.count(CONTAINER) != 1:
        raise ValueError("Local AX template source differs from the pinned version; inspect the checkout")
    return source.replace(MARKER, HELPER + MARKER, 1).replace(CONTAINER, SECURITY + CONTAINER, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--check", type=Path, metavar="DEPLOYED_VERSION_FILE")
    args = parser.parse_args()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=args.checkout,
                          capture_output=True, text=True, check=True).stdout.strip()
    if head != AX_COMMIT:
        raise ValueError("Unexpected AX checkout; tool-user patch requires the pinned v0.3.1 commit")
    path = args.checkout / "internal/substrate/client.go"
    source = path.read_text(encoding="utf-8")
    updated = patched_source(source)
    if args.check:
        if updated != source or not args.check.is_file() \
                or args.check.read_text(encoding="utf-8").strip() != VERSION:
            raise SystemExit(1)
        return
    if updated != source:
        path.write_text(updated, encoding="utf-8")
        subprocess.run(["gofmt", "-w", str(path)], check=True)
    print(VERSION)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit("AX controller tool-user setup failed: " +
                         (str(exc) if isinstance(exc, ValueError) else "check local AX source and tools"))
