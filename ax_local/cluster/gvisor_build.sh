#!/usr/bin/env bash
set -euo pipefail

COMMIT="$1"
VERSION="$2"
mkdir -p /src /build-home
git -C /src init -q
git -C /src remote add origin https://github.com/google/gvisor.git
git -C /src fetch -q --depth=1 origin "$COMMIT"
git -C /src checkout -q --detach FETCH_HEAD
test "$(git -C /src rev-parse HEAD)" = "$COMMIT"

# Pin the version stamp too: a shallow checkout has no release tags.
printf '#!/bin/sh\nprintf "STABLE_VERSION %%s\\n" "%s"\n' "$VERSION" > /build-home/workspace-status.sh
chmod 755 /build-home/workspace-status.sh
useradd --uid 10001 --home-dir /build-home --shell /bin/bash builder
chown -R 10001:10001 /src /build-home
cd /src
runuser -u builder -- env HOME=/build-home USER=builder \
  bazel --batch --host_jvm_args=-Xmx768m build --config=aarch64 -c opt \
  --jobs=2 --local_resources=memory=2048 \
  --workspace_status_command=/build-home/workspace-status.sh \
  //debian:gvisor-release-tar-zstd
cp -L /src/bazel-bin/debian/gvisor.tar.zstd /out/gvisor.tar.zstd.partial
