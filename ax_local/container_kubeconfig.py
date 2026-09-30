"""Compatibility entry point for preparing the container kubeconfig."""

import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ax_local.cluster.container_kubeconfig import main, rewrite_for_container


if __name__ == "__main__":
    main(sys.argv[1:])
