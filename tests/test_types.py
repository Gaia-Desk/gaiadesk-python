"""The types: generated from the CLI's JSON Schema (types_generated.py),
re-exported with the SDK's earlier names; the client never needs them at
runtime."""

import os
import subprocess
import sys
import unittest

import helpers

from gaiadesk import types as T
from gaiadesk import types_generated as G


class TypesTest(unittest.TestCase):
    def test_the_generated_shapes_are_exported(self):
        for name in ("ExecResult", "ExecExit", "ExecEvent", "Error", "ErrorEnvelope", "ErrorKind", "Job", "JobList", "JobLogs",
                     "TokenList", "TokenInfo", "AuditLog", "AuditEvent", "MintResult", "DeviceList", "Disconnected", "StatsReport",
                     "Measurement", "VersionInfo", "CopyResult", "ForwardListening", "MeshStatus", "Shell"):
            self.assertIs(getattr(T, name), getattr(G, name), name)

    def test_the_earlier_names_are_aliases(self):
        self.assertIs(T.JobInfo, G.Job)
        self.assertIs(T.CpSummary, G.CopyResult)
        self.assertIs(T.CpFailure, G.CopyFailure)
        self.assertIs(T.DeviceRow, G.Device)
        self.assertIs(T.DevicesResult, G.DeviceList)
        self.assertIs(T.MeasureResult, G.Measurement)
        for name in ("DeskStats", "ReachSuccess", "ReachFailure", "TokenCreateResult", "ExecEvent_Exit", "ExecStreamEvent"):
            self.assertTrue(hasattr(T, name), name)

    def test_the_client_imports_without_typing_extensions(self):
        src = os.path.join(os.path.dirname(helpers.HERE), "src")
        code = ("import sys; sys.modules['typing_extensions'] = None; sys.path.insert(0, %r)\n"
                "import gaiadesk, gaiadesk.aio, gaiadesk.client\n"
                "print(gaiadesk.GaiaDesk.__name__)" % src)
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "GaiaDesk"), p.stderr)


if __name__ == "__main__":
    unittest.main()
