"""Shared test setup: the package from src/, and a client wired to the fake CLI."""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

FAKE = os.path.join(HERE, "fixtures", "fake_cli.py")
FAKE_OLD = os.path.join(HERE, "fixtures", "fake_cli_old.py")  # a gaiadesk-cli from before 0.10.324
OK, OFFLINE, REFUSED, USAGE, PLAIN = "123456789", "234567890", "345678901", "desk-usage", "desk-plain"


def base_env(log):
    env = {"PATH": os.environ.get("PATH", ""), "FAKE_LOG": log}
    for k in ("SystemRoot", "SYSTEMROOT", "TEMP", "TMP"):  # Windows needs these to start Python
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def setup(cls, old=False, **opts):
    """A client of class `cls` on the fake CLI (`old`: one from before 0.10.324), and a function returning the calls it made."""
    d = tempfile.mkdtemp(prefix="gaiadesk-sdk-")
    log = os.path.join(d, "calls.jsonl")
    env = opts.pop("env", None) or base_env(log)
    env.setdefault("FAKE_LOG", log)
    client = cls(cli=[sys.executable, FAKE_OLD if old else FAKE], env=env, **opts)

    def calls():
        if not os.path.exists(log):
            return []
        with open(log) as f:
            return [json.loads(l) for l in f if l.strip()]

    return client, calls


def desk_calls(calls):
    """The calls that were not the SDK asking the CLI what it can do (``--version --json``)."""
    return [c for c in calls() if c["argv"] != ["--version", "--json"]]
