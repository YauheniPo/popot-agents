"""Adapt pinned local AX templates for Popot tool isolation and session snapshots.

AX v0.3.1 has no Task securityContext. Its Substrate templates start as UID 0
without SETUID/SETGID; our tool-enabled image needs those two to drop privileges.
Its default DATA snapshots omit process memory; Popot sessions opt into FULL.
"""

import argparse
import hashlib
import subprocess
from pathlib import Path

if __package__:
    from .build_versions import BUILD_VERSIONS
else:
    from build_versions import BUILD_VERSIONS

AX_COMMIT = BUILD_VERSIONS['AX_COMMIT']
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
SNAPSHOT_HELPER = '''// popotSnapshotScope preserves live session memory only for opted-in Popot images.
func popotSnapshotScope(image string, env map[string]string) ateapipb.SnapshotContentScope {
	if env["POPOT_SESSION_SNAPSHOT"] == "full" && strings.HasPrefix(image, "localhost:5001/popot-agent-ax@sha256:") {
		return ateapipb.SnapshotContentScope_SNAPSHOT_CONTENT_SCOPE_FULL
	}
	return ateapipb.SnapshotContentScope_SNAPSHOT_CONTENT_SCOPE_DATA
}

'''
PAUSE = '\t\t\tOnPause:         ateapipb.SnapshotContentScope_SNAPSHOT_CONTENT_SCOPE_DATA,'
COMMIT = '\t\t\tOnCommit:        ateapipb.SnapshotContentScope_SNAPSHOT_CONTENT_SCOPE_DATA,'
SNAPSHOT_SCOPE = 'popotSnapshotScope(image, envMap)'
VERSION = hashlib.sha256((AX_COMMIT + HELPER + SECURITY + SNAPSHOT_HELPER + SNAPSHOT_SCOPE).encode()).hexdigest()


def patched_source(source: str) -> str:
    if "func popotToolSecurityContext(" in source:
        if source.count(HELPER) != 1 or source.count(SECURITY) != 1:
            raise ValueError("Local AX tool-user patch has unexpected changes; inspect the checkout")
    else:
        if source.count(MARKER) != 1 or source.count(CONTAINER) != 1:
            raise ValueError("Local AX template source differs from the pinned version; inspect the checkout")
        source = source.replace(MARKER, HELPER + MARKER, 1).replace(CONTAINER, SECURITY + CONTAINER, 1)
    if "func popotSnapshotScope(" in source:
        if source.count(SNAPSHOT_HELPER) != 1 or source.count(SNAPSHOT_SCOPE) != 2:
            raise ValueError("Local AX snapshot patch has unexpected changes; inspect the checkout")
        return source
    if source.count(MARKER) != 1 or source.count(PAUSE) != 1 or source.count(COMMIT) != 1:
        raise ValueError("Local AX snapshot source differs from the pinned version; inspect the checkout")
    return (source.replace(MARKER, SNAPSHOT_HELPER + MARKER, 1)
            .replace(PAUSE, PAUSE.replace('ateapipb.SnapshotContentScope_SNAPSHOT_CONTENT_SCOPE_DATA', SNAPSHOT_SCOPE), 1)
            .replace(COMMIT, COMMIT.replace('ateapipb.SnapshotContentScope_SNAPSHOT_CONTENT_SCOPE_DATA', SNAPSHOT_SCOPE), 1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--check", type=Path, metavar="DEPLOYED_VERSION_FILE")
    args = parser.parse_args()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=args.checkout,
                          capture_output=True, text=True, check=True).stdout.strip()
    if head != AX_COMMIT:
        raise ValueError(f"Unexpected AX checkout; tool-user patch requires the pinned {BUILD_VERSIONS['AX_VERSION']} commit")
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
