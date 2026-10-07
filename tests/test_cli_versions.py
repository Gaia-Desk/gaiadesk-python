"""What changed in gaiadesk-cli 0.10.324, against a fake of it AND a fake of
the CLI before it (fixtures/fake_cli_old.py): feature detection, the one
error envelope, ``--cwd``, ``exec --json-stream``, the object-shaped lists."""

import asyncio
import unittest

import helpers
from helpers import OFFLINE, OK, PLAIN, REFUSED, desk_calls

from gaiadesk import (
    AsyncGaiaDesk,
    CommandError,
    GaiaDesk,
    GaiaDeskError,
    OperationFailedError,
    RefusedError,
    UnreachableError,
    UsageError,
)
from gaiadesk._core import clear_feature_cache
from gaiadesk.stream import AsyncJsonExecStream, CliStream, JsonExecStream


def text_of(stream):
    out, err = "", ""
    for name, t in stream.text():
        if name == "stdout":
            out += t
        else:
            err += t
    return out, err


class FeatureDetection(unittest.TestCase):
    def setUp(self):
        clear_feature_cache()

    def test_a_new_cli_lists_its_features_once_per_cli(self):
        gd, calls = helpers.setup(GaiaDesk)
        info = gd.cli_version_info()
        self.assertEqual((info["name"], info["version"]), ("gaiadesk-cli", "0.10.324"))
        self.assertIn("v2", info["json_shapes"])
        self.assertTrue({"exec_json_stream", "exec_cwd", "run_cwd", "json_error_envelope"} <= gd.cli_features())
        # Another client on the same CLI asks nothing more.
        gd2, calls2 = helpers.setup(GaiaDesk)
        gd2.cli_features()
        gd2.exec(OK, "x", cwd="/srv")
        self.assertEqual([c["argv"] for c in calls()], [["--version", "--json"]])
        self.assertEqual([c["argv"][0] for c in calls2()], ["exec"])

    def test_an_old_cli_has_no_features(self):
        gd, calls = helpers.setup(GaiaDesk, old=True)
        self.assertIsNone(gd.cli_version_info())
        self.assertEqual(gd.cli_features(), frozenset())
        self.assertEqual(gd.version(), "gaiadesk-cli 0.10.323")

    def test_async(self):
        async def go():
            gd, _ = helpers.setup(AsyncGaiaDesk)
            self.assertIn("exec_cwd", await gd.cli_features())
            old, _ = helpers.setup(AsyncGaiaDesk, old=True)
            self.assertIsNone(await old.cli_version_info())

        asyncio.run(go())


class Cwd(unittest.TestCase):
    def setUp(self):
        clear_feature_cache()

    def test_exec_and_run_pass_cwd(self):
        gd, calls = helpers.setup(GaiaDesk)
        r = gd.exec(OK, "make", cwd="/srv/app")
        self.assertIn("cwd: /srv/app", r["stdout"])
        gd.run_job(OK, "build", ["make"], cwd="src")
        argvs = [c["argv"] for c in desk_calls(calls)]
        self.assertEqual(argvs[0], ["exec", "--desk-id", OK, "--quiet", "--json", "--no-stdin", "--cwd", "/srv/app", "--", "make"])
        self.assertEqual(argvs[1], ["run", "--detach", "--name", "build", "--desk-id", OK, "--cwd", "src", "--json", "--", "make"])

    def test_an_old_cli_is_never_sent_cwd(self):
        gd, calls = helpers.setup(GaiaDesk, old=True)
        for run in (lambda: gd.exec(OK, "make", cwd="/srv"), lambda: gd.run_job(OK, "b", "make", cwd="/srv"),
                    lambda: gd.exec_stream(OK, "make", cwd="/srv")):
            with self.assertRaises(UsageError) as cm:
                run()
            self.assertEqual(cm.exception.kind, "usage")
            self.assertIn("0.10.324", str(cm.exception))
        self.assertEqual(desk_calls(calls), [], "nothing ran")
        # Without cwd an old CLI is never asked for its features.
        gd2, calls2 = helpers.setup(GaiaDesk, old=True)
        gd2.exec(OK, "make")
        gd2.run_job(OK, "b", "make")
        self.assertNotIn(["--version", "--json"], [c["argv"] for c in calls2()])

    def test_bad_cwd(self):
        gd, _ = helpers.setup(GaiaDesk)
        for bad in ("", "  "):
            with self.assertRaises(UsageError):
                gd.exec(OK, "x", cwd=bad)

    def test_async(self):
        async def go():
            gd, calls = helpers.setup(AsyncGaiaDesk)
            r = await gd.exec(OK, "make", cwd="/srv/app")
            self.assertIn("cwd: /srv/app", r["stdout"])
            await gd.run_job(OK, "build", "make", cwd="/srv")
            self.assertIn("--cwd", desk_calls(calls)[-1]["argv"])
            s = await gd.exec_stream(OK, "make", cwd="/w")
            out = "".join([t async for n, t in s.text() if n == "stdout"])
            self.assertIn("cwd: /w", out)
            old, ocalls = helpers.setup(AsyncGaiaDesk, old=True)
            with self.assertRaises(UsageError):
                await old.run_job(OK, "b", "make", cwd="/srv")
            self.assertEqual(desk_calls(ocalls), [])

        asyncio.run(go())


class JsonStream(unittest.TestCase):
    def setUp(self):
        clear_feature_cache()

    def test_events_when_the_cli_has_them(self):
        gd, calls = helpers.setup(GaiaDesk)
        s = gd.exec_stream(OK, "exit 2")
        self.assertIsInstance(s, JsonExecStream)
        self.assertEqual(text_of(s), ("part1 part2 exit 2\n", "warn\n"))
        e = s.wait()
        self.assertEqual(e.exit_code, 2)
        self.assertEqual((s.result["event"], s.result["exit"], s.result["route"], s.result["shell"]), ("exit", 2, "LAN", "/bin/zsh -l -c"))
        self.assertIn("--json-stream", desk_calls(calls)[0]["argv"])
        self.assertNotIn("--json", desk_calls(calls)[0]["argv"])

    def test_wait_first_keeps_the_output(self):
        gd, _ = helpers.setup(GaiaDesk)
        s = gd.exec_stream(OK, "make")
        self.assertEqual(s.wait().exit_code, 0)
        self.assertEqual(text_of(s)[0], "part1 part2 make\n")

    def test_stdin_and_errors(self):
        gd, _ = helpers.setup(GaiaDesk)
        s = gd.exec_stream(OK, "cat", stdin=True)
        s.write("hello")
        s.end()
        self.assertIn("stdin: hello", text_of(s)[0])
        self.assertEqual(s.wait().exit_code, 0)
        s = gd.exec_stream(OFFLINE, "x")
        self.assertEqual(text_of(s), ("", ""))
        e = s.wait()
        self.assertEqual((e.exit_code, e.stderr_tail), (255, "desk %s is offline (last seen 4 min ago)" % OFFLINE))
        self.assertEqual((s.result["event"], s.result["error"]["kind"], s.result["error"]["reason"]), ("error", "unreachable", "offline"))

    def test_plain_when_asked_or_old(self):
        gd, calls = helpers.setup(GaiaDesk)
        s = gd.exec_stream(OK, "make", json_stream=False)
        self.assertIsInstance(s, CliStream)
        self.assertEqual(text_of(s), ("part1 part2 make\n", "warn\n"))
        self.assertIsNone(s.result)
        self.assertEqual(s.wait().exit_code, 0)
        old, ocalls = helpers.setup(GaiaDesk, old=True)
        s = old.exec_stream(OK, "exit 3")
        self.assertIsInstance(s, CliStream)
        self.assertEqual(text_of(s)[0], "part1 part2 exit 3\n")
        self.assertEqual(s.wait().exit_code, 3)
        self.assertNotIn("--json-stream", desk_calls(ocalls)[0]["argv"])
        with self.assertRaises(UsageError):
            old.exec_stream(OK, "x", json_stream=True)

    def test_async(self):
        async def go():
            gd, _ = helpers.setup(AsyncGaiaDesk)
            s = await gd.exec_stream(OK, "exit 4")
            self.assertIsInstance(s, AsyncJsonExecStream)
            chunks = [c async for c in s]
            self.assertEqual(b"".join(c.data for c in chunks if c.stream == "stdout"), b"part1 part2 exit 4\n")
            self.assertEqual((await s.wait()).exit_code, 4)
            self.assertEqual(s.result["exit"], 4)
            old, _ = helpers.setup(AsyncGaiaDesk, old=True)
            s = await old.exec_stream(OK, "exit 1")
            self.assertEqual((await s.wait()).exit_code, 1)
            self.assertIsNone(s.result)

        asyncio.run(go())


class ShellCwd(unittest.TestCase):
    def setUp(self):
        clear_feature_cache()

    def test_shell_passes_cwd(self):
        gd, calls = helpers.setup(GaiaDesk)
        r = gd.shell(OK, "make\n", cwd="/srv/app")
        self.assertIn("cwd: /srv/app", r["stdout"])
        gd.shell_stream(OK, "make\n", cwd="proj").wait()
        argvs = [c["argv"] for c in desk_calls(calls)]
        self.assertEqual(argvs[0], ["shell", "--desk-id", OK, "--quiet", "--json", "--cwd", "/srv/app"])
        self.assertEqual(argvs[1], ["shell", "--desk-id", OK, "--quiet", "--cwd", "proj"])

    def test_an_old_cli_is_never_sent_shell_cwd(self):
        gd, calls = helpers.setup(GaiaDesk, old=True)
        for run in (lambda: gd.shell(OK, "make", cwd="/srv"), lambda: gd.shell_stream(OK, "make", cwd="/srv")):
            with self.assertRaises(UsageError) as cm:
                run()
            self.assertIn("shell_cwd", str(cm.exception))
        with self.assertRaises(UsageError):
            gd.shell(OK, "make", cwd=" ")
        self.assertEqual(desk_calls(calls), [], "nothing ran")

    def test_async(self):
        async def go():
            gd, calls = helpers.setup(AsyncGaiaDesk)
            r = await gd.shell(OK, "make\n", cwd="/w")
            self.assertIn("cwd: /w", r["stdout"])
            old, ocalls = helpers.setup(AsyncGaiaDesk, old=True)
            with self.assertRaises(UsageError):
                await old.shell_stream(OK, "make", cwd="/w")
            self.assertEqual(desk_calls(ocalls), [])

        asyncio.run(go())


class JsonForms(unittest.TestCase):
    """logs / mesh ip / disconnect / agent-connect: ``--json`` where the CLI lists it, its text before; the same results."""

    def setUp(self):
        clear_feature_cache()

    def test_same_results_either_way(self):
        for old in (False, True):
            with self.subTest(old=old):
                gd, calls = helpers.setup(GaiaDesk, old=old, agent_token="gdagt_x")
                self.assertEqual(gd.job_logs(OK, "build"), "line1\nline2\n")
                self.assertEqual(gd.job_logs(OK, "build", tail=5), "tail\n")
                with self.assertRaises(OperationFailedError):
                    gd.job_logs(OK, "nope")
                self.assertEqual(text_of_stream(gd.follow_job_logs(OK, "build")), "one\ntwo\nthree\n")
                self.assertEqual(gd.mesh_ip(OK), "100.64.0.2")
                with self.assertRaises(OperationFailedError):
                    gd.mesh_ip(OFFLINE)
                self.assertEqual(gd.disconnect(OK), {"closed": [OK]})
                self.assertEqual(gd.agent_connect(OK), "agent session open on desk %s: screenshot 1280x800" % OK)
                sent_json = ["--json" in c["argv"] for c in desk_calls(calls)]
                self.assertEqual(set(sent_json), {not old}, "--json only to a CLI that lists it")

    def test_async(self):
        async def go():
            gd, calls = helpers.setup(AsyncGaiaDesk, agent_token="gdagt_x")
            self.assertEqual(await gd.job_logs(OK, "build"), "line1\nline2\n")
            s = await gd.follow_job_logs(OK, "build")
            self.assertEqual("".join([t async for n, t in s.text() if n == "stdout"]), "one\ntwo\nthree\n")
            await s.wait()
            self.assertEqual(await gd.mesh_ip(OK), "100.64.0.2")
            self.assertEqual(await gd.disconnect(), {"closed": [OK]})
            self.assertIn("1280x800", await gd.agent_connect(OK))
            self.assertTrue(all("--json" in c["argv"] for c in desk_calls(calls)))

        asyncio.run(go())


def text_of_stream(s):
    out = "".join(t for n, t in s.text() if n == "stdout")
    s.wait()
    return out


class BothClis(unittest.TestCase):
    """The same calls give the same results and errors on either CLI."""

    def each(self, **opts):
        return [helpers.setup(GaiaDesk, **opts)[0], helpers.setup(GaiaDesk, old=True, **opts)[0]]

    def test_lists_are_lists(self):
        for gd in self.each(code="pw"):
            self.assertEqual([j["name"] for j in gd.jobs(OK)], ["build", "old"])
            self.assertEqual(gd.list_tokens(OK)[0]["label"], "bot")
            self.assertEqual(gd.audit(OK)[0]["action"], "exec.end")

    def test_exec_errors(self):
        for gd in self.each():
            with self.assertRaises(UnreachableError) as cm:
                gd.exec(OFFLINE, "x")
            self.assertEqual((cm.exception.kind, cm.exception.exit_code), ("offline", 255))
            with self.assertRaises(RefusedError) as cm:
                gd.exec(REFUSED, "x")
            self.assertEqual((cm.exception.kind, cm.exception.exit_code, cm.exception.desk), ("refused", 254, REFUSED))
            t = gd.exec(OK, "sleep")  # timed out: a result, not an error
            self.assertEqual((t["exit"], t["timed_out"]), (124, True))
            with self.assertRaises(CommandError):
                gd.exec(OK, "sleep", check=True)

    def test_operation_errors(self):
        for gd in self.each():
            with self.assertRaises(RefusedError):
                gd.run_job(REFUSED, "b", "make")
            with self.assertRaises(RefusedError):
                gd.upload("a", REFUSED, "x/")
            with self.assertRaises(OperationFailedError) as cm:
                gd.kill_job(OK, "nope")
            self.assertEqual((str(cm.exception), cm.exception.kind), ("no job named nope", "failed"))
            with self.assertRaises(RefusedError):
                gd.list_tokens(OK)
            with self.assertRaises(GaiaDeskError) as cm:
                gd.stats(PLAIN)
            self.assertEqual((str(cm.exception), cm.exception.desk), ("the desk did not answer", PLAIN))
        gd2, _ = helpers.setup(GaiaDesk, code="pw")
        with self.assertRaises(OperationFailedError):
            gd2.revoke_token(OK, "ghost")

    def test_the_envelope_carries_kind_reason_and_desk(self):
        gd, _ = helpers.setup(GaiaDesk)
        with self.assertRaises(UnreachableError) as cm:
            gd.stats(PLAIN)
        e = cm.exception
        self.assertEqual((e.kind, e.reason, e.desk, e.exit_code), ("timeout", "timeout", PLAIN, 255))
        self.assertEqual(e.json, {"error": {"kind": "unreachable", "message": "the desk did not answer", "reason": "timeout", "desk": PLAIN}})
        with self.assertRaises(RefusedError) as cm:
            gd.run_job(REFUSED, "b", "make")
        self.assertEqual((cm.exception.kind, cm.exception.desk), ("refused", REFUSED))
        # The old CLI: no kind, so the exit code and the stderr sentence decide.
        old, _ = helpers.setup(GaiaDesk, old=True)
        with self.assertRaises(GaiaDeskError) as cm:
            old.stats(PLAIN)
        self.assertEqual((cm.exception.kind, cm.exception.reason), ("cli_error", None))


if __name__ == "__main__":
    unittest.main()
