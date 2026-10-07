"""fake_cli.py as a gaiadesk-cli too old to answer ``--version --json``
(FAKE_CLI=old), at its own path: the SDK caches what a CLI can do per path."""

import os
import runpy
import sys

os.environ["FAKE_CLI"] = "old"
sys.argv[0] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_cli.py")
runpy.run_path(sys.argv[0], run_name="__main__")
