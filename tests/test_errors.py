"""The one place that reads gaiadesk-cli's error envelopes, and the mapping
from kinds and exit codes to error classes."""

import unittest

import helpers  # noqa: F401  (puts src/ on sys.path)

from gaiadesk import (
    ConnectionLostError,
    GaiaDeskError,
    OperationFailedError,
    ProtocolError,
    RefusedError,
    UnreachableError,
    UsageError,
    error_envelope,
)
from gaiadesk.errors import ErrorEnvelope, error_from_run, last_stderr_line


class ErrorEnvelopeTest(unittest.TestCase):
    def test_the_envelope(self):
        self.assertEqual(
            error_envelope({"error": {"kind": "unreachable", "message": "desk 123456789 is offline", "reason": "offline", "desk": "123456789"}}),
            ErrorEnvelope("unreachable", "desk 123456789 is offline", "offline", "123456789"),
        )
        self.assertEqual(error_envelope({"error": {"kind": "failed", "message": "no job named x"}}), ErrorEnvelope("failed", "no job named x"))
        # exec's object with the envelope's error in it
        self.assertEqual(error_envelope({"exit": 254, "error": {"kind": "refused", "message": "no", "reason": None}}).kind, "refused")

    def test_a_missing_message_is_empty(self):
        self.assertEqual(error_envelope({"error": {"kind": "usage"}}), ErrorEnvelope("usage", ""))

    def test_results_are_not_errors(self):
        for ok in (
            None,
            "text",
            [{"name": "job"}],
            {"exit": 0, "error": None, "stdout": ""},  # exec success carries "error": null
            {"error": ""},
            {"error": "text is not an envelope"},
            {"refused": "not an envelope either"},
            {"desk": "1", "ok": True, "message": "revoked"},
            {"devices": [], "sources": [], "notes": []},
        ):
            self.assertIsNone(error_envelope(ok), ok)


class ErrorFromRunTest(unittest.TestCase):
    def test_the_six_kinds(self):
        for kind, cls in (("usage", UsageError), ("refused", RefusedError), ("unreachable", UnreachableError),
                          ("connection_lost", ConnectionLostError), ("failed", OperationFailedError), ("protocol", ProtocolError)):
            e = error_from_run(255, "", ["x"], {"error": {"kind": kind, "message": "m"}})
            self.assertIs(type(e), cls, kind)
            self.assertEqual((e.kind, e.reason, e.desk, str(e)), (kind, None, None, "m"))

    def test_the_reason_and_desk(self):
        e = error_from_run(255, "", [], {"error": {"kind": "unreachable", "message": "m", "reason": "not_online", "desk": "234567890"}})
        self.assertIsInstance(e, UnreachableError)
        self.assertEqual((e.kind, e.reason, e.desk), ("not_online", "not_online", "234567890"))
        local = error_from_run(1, "", [], {"error": {"kind": "failed", "message": "m", "reason": "local"}})
        self.assertIsInstance(local, OperationFailedError)
        self.assertEqual(local.kind, "local")
        # A reason this SDK does not know keeps the envelope's kind.
        odd = error_from_run(255, "", [], {"error": {"kind": "unreachable", "message": "m", "reason": "solar_flare"}})
        self.assertEqual((odd.kind, odd.reason), ("unreachable", "solar_flare"))
        # No message: the stderr sentence.
        self.assertEqual(str(error_from_run(255, "gaiadesk-cli: why\n", [], {"error": {"kind": "usage"}})), "why")
        # A reply without an envelope still names its desk.
        self.assertEqual(error_from_run(1, "", [], {"desk": "345678901", "ok": False, "message": "m"}).desk, "345678901")

    def test_a_kind_decides_the_class(self):
        e = error_from_run(255, "", ["exec"], {"error": {"kind": "unreachable", "message": "m", "reason": "not_online"}})
        self.assertIsInstance(e, UnreachableError)
        self.assertEqual((e.kind, e.exit_code), ("not_online", 255))
        self.assertIsInstance(error_from_run(255, "", [], {"error": {"kind": "usage", "message": "m"}}), UsageError)
        self.assertIsInstance(error_from_run(255, "", [], {"error": {"kind": "refused", "message": "m"}}), RefusedError)
        self.assertIsInstance(error_from_run(255, "", [], {"error": {"kind": "connection_lost", "message": "m"}}), ConnectionLostError)
        other = error_from_run(255, "", [], {"error": {"kind": "local", "message": "m"}})
        self.assertIs(type(other), GaiaDeskError)
        self.assertEqual(other.kind, "local")

    def test_otherwise_the_exit_code_decides_with_the_best_message(self):
        refused = error_from_run(254, "gaiadesk-cli: turned off\n", [], None)
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
