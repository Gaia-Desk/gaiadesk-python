"""Shared test setup: the package from src/, and a client wired to the fake CLI."""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

FAKE = os.path.join(HERE, "fixtures", "fake_cli.py")
OK, OFFLINE, REFUSED, USAGE, PLAIN = "100000001", "100000002", "100000003", "100000004", "100000005"


def base_env(log):
    env = {"PATH": os.environ.get("PATH", ""), "FAKE_LOG": log}
    for k in ("SystemRoot", "SYSTEMROOT", "TEMP", "TMP"):  # Windows needs these to start Python
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def setup(cls, **opts):
    """A client of class `cls` on the fake CLI, and a function returning the calls it made."""
    d = tempfile.mkdtemp(prefix="gaiadesk-sdk-")
    log = os.path.join(d, "calls.jsonl")
    env = opts.pop("env", None) or base_env(log)
    env.setdefault("FAKE_LOG", log)
    client = cls(cli=[sys.executable, FAKE], env=env, **opts)

    def calls():
        if not os.path.exists(log):
            return []
        with open(log) as f:
            return [json.loads(l) for l in f if l.strip()]

    return client, calls
