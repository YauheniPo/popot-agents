"""Start the pinned Phoenix with deployment-owned catalogs and request defaults."""
from pathlib import Path
import runpy
import sys

# Do not put evals/phoenix.py ahead of the installed phoenix package.
sys.path[0] = str(Path(__file__).resolve().parents[1])
from evals.phoenix_providers import install_runtime, load_settings

if __name__ == '__main__':
    install_runtime(load_settings())
    sys.argv = ['phoenix', 'serve']
    runpy.run_module('phoenix.server.main', run_name='__main__')
