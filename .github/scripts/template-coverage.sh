#!/usr/bin/env bash
# Run the Go test in its pinned upstream package and report repository-relative paths.
set -euo pipefail
checkout=$1
pin=$(python3 -c 'from ax_local.cluster.substrate_api import PIN; print(PIN)')
if [[ $(git -C "$checkout" rev-parse HEAD) != "$pin" ]]; then
    echo "Substrate revision differs from the build pin" >&2
    exit 1
fi
directory="$checkout/cmd/ateapi/internal/controlapi"
python3 - "$directory/actor_template.go" <<'PY'
from pathlib import Path
import sys
from ax_local.cluster.substrate_api import patched_source

source = Path(sys.argv[1])
source.write_text(patched_source(source.read_text()))
PY
cp ax_local/cluster/template_cleanup_go/*.go "$directory/"
mkdir -p reports
profile=$(mktemp)
trap 'rm -f "$profile"' EXIT
go -C "$checkout" test ./cmd/ateapi/internal/controlapi -run '^TestPopotTemplateDeletion$' -count=1 -coverprofile="$profile"
awk '/^mode:/ {print; next}
     /\/popot_template_cleanup\.go:/ {sub(/^.*\/popot_template_cleanup\.go:/, "ax_local/cluster/template_cleanup_go/popot_template_cleanup.go:"); print; found=1}
     END {if (!found) exit 1}' "$profile" > reports/template-coverage.out
