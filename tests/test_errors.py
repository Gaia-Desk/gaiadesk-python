"""The one place that reads gaiadesk-cli's error envelopes, and the mapping
from kinds and exit codes to error classes."""

import unittest

import helpers  # noqa: F401  (puts src/ on sys.path)

from gaiadesk import (
    ConnectionLostError,
    GaiaDeskError,
    OperationFailedError,
    RefusedError,
    UnreachableError,
    UsageError,
    error_envelope,
)
from gaiadesk.errors import ErrorEnvelope, error_from_run, last_stderr_line


class ErrorEnvelopeTest(unittest.TestCase):
    def test_every_shape_the_cli_prints_today(self):
        self.assertEqual(error_envelope({"error": {"kind": "offline", "message": "desk is offline"}}), ErrorEnvelope("offline", "desk is offline"))
        self.assertEqual(error_envelope({"error": {"kind": "usage"}}), ErrorEnvelope("usage", ""))
        self.assertEqual(error_envelope({"error": "no job named x"}), ErrorEnvelope(None, "no job named x"))
        self.assertEqual(error_envelope({"desk": "1", "error": "the desk did not answer"}), ErrorEnvelope(None, "the desk did not answer"))
        self.assertEqual(error_envelope({"refused": "file transfer is turned off for you"}), ErrorEnvelope(None, "file transfer is turned off for you"))

    def test_results_are_not_errors(self):
        for ok in (
            None,
            "text",
            [{"name": "job"}],
            {"exit": 0, "error": None, "stdout": ""},  # exec success carries "error": null
            {"error": ""},
            {"desk": "1", "ok": True, "message": "revoked"},
            {"devices": [], "sources": [], "notes": []},
        ):
            self.assertIsNone(error_envelope(ok), ok)


class ErrorFromRunTest(unittest.TestCase):
    def test_a_kind_decides_the_class(self):
        e = error_from_run(255, "", ["exec"], {"error": {"kind": "not_online", "message": "m"}})
        self.assertIsInstance(e, UnreachableError)
        self.assertEqual((e.kind, e.exit_code), ("not_online", 255))
        self.assertIsInstance(error_from_run(255, "", [], {"error": {"kind": "usage", "message": "m"}}), UsageError)
        self.assertIsInstance(error_from_run(255, "", [], {"error": {"kind": "refused", "message": "m"}}), RefusedError)
        self.assertIsInstance(error_from_run(255, "", [], {"error": {"kind": "connection_lost", "message": "m"}}), ConnectionLostError)
        other = error_from_run(255, "", [], {"error": {"kind": "local", "message": "m"}})
        self.assertIs(type(other), GaiaDeskError)
        self.assertEqual(other.kind, "local")

    def test_otherwise_the_exit_code_decides_with_the_best_message(self):
        refused = error_from_run(254, "", [], {"refused": "turned off"})
        self.assertIsInstance(refused, RefusedError)
        self.assertEqual(str(refused), "turned off")
        failed = error_from_run(1, "gaiadesk-cli: no job named x\n", [], None)
        self.assertIsInstance(failed, OperationFailedError)
        self.assertEqual(str(failed), "no job named x")
        self.assertEqual(str(error_from_run(1, "", [], {"desk": "1", "ok": False, "message": "not your desk"})), "not your desk")
        bare = error_from_run(255, "", ["x"], None)
        self.assertEqual((bare.kind, str(bare)), ("cli_error", "gaiadesk-cli exited with 255"))

    def test_last_stderr_line(self):
        self.assertEqual(last_stderr_line("gaiadesk-cli: desk 1 is offline\n(see `gaiadesk-cli exec --help`)\n"), "desk 1 is offline")
        self.assertEqual(last_stderr_line(""), "")


if __name__ == "__main__":
    unittest.main()
