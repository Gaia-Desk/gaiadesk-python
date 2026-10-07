"""The native backend against a REAL gaiadesk_native build: its test backend
(fake desks of its own) behind the real extension.
Skipped unless gaiadesk_native is installed AND is that test build
(``build_info()["backend"] == "stub"``; a release build talks to real desks)."""

import asyncio
import os
import tempfile
import unittest

import helpers  # noqa: F401  (puts src/ on sys.path)

from gaiadesk import AsyncGaiaDesk, GaiaDesk, OperationFailedError, RefusedError, UnreachableError, UsageError

try:
    import gaiadesk_native

    STUB = gaiadesk_native.build_info().get("backend") == "stub"
except Exception:  # not installed, or no binary for this machine
    STUB = False



def stub_desk(n):
    """The fake desks of gaiadesk_native's test build (its stub backend fixes
    them as 10000000<n>; no real desk has such an id): 1 fine, 2 offline,
    3 refuses, 4 a usage error, 5 drops the connection."""
    return "10000000%d" % n


OK, OFFLINE, REFUSED, USAGE = stub_desk(1), stub_desk(2), stub_desk(3), stub_desk(4)


def gd(**kw):
    return GaiaDesk(backend="native", env={"PATH": os.environ.get("PATH", "")}, code="pw", agent_token="gdagt_x", **kw)


@unittest.skipUnless(STUB, "needs a test build of gaiadesk_native installed")
class RealNative(unittest.TestCase):
    def test_exec_and_results(self):
        g = gd()
        self.assertEqual(g.backend, "native")
        self.assertRegex(g.version(), r"^gaiadesk-native \d+\.\d+\.\d+$")
        r = g.exec(OK, "make; exit 3", stdin="hi", timeout="10m")
        self.assertEqual((r["exit"], r["stdout"]), (3, "ran: make; exit 3\nstdin: hi\n"))
        self.assertEqual(g.shell(OK, "echo hi")["stdout"], "script: echo hi\n")
        self.assertEqual(g.stats(OK)["hostname"], "build-box")
        self.assertEqual(len(g.devices(probe=True)["devices"]), 2)

    def test_errors(self):
        g = gd()
        with self.assertRaises(UnreachableError) as c:
            g.exec(OFFLINE, "x")
        self.assertEqual(c.exception.kind, "offline")
        with self.assertRaises(RefusedError) as c:
            g.exec(REFUSED, "x")
        self.assertEqual(c.exception.exit_code, 254)
        with self.assertRaises(UsageError):
            g.exec(USAGE, "x")
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.txt"), "w") as f:
                f.write("hello")
            c2 = gd(cwd=d)
            self.assertEqual(c2.upload("a.txt", OK, "/tmp/a")["bytes"], 5)
            with self.assertRaises(OperationFailedError) as c:
                c2.upload("a.txt", OK, "/readonly/a")
            self.assertEqual(len(c.exception.json["failed"]), 1)

    def test_streams_jobs_forward_agent(self):
        g = gd()
        s = g.exec_stream(OK, "hostname")
        self.assertEqual("".join(t for n, t in s.text() if n == "stdout"), "ran: hostname\n")
        self.assertEqual(s.wait().exit_code, 0)
        g.run_job(OK, "forever", "yes")
        f = g.follow_job_logs(OK, "forever")
        for _ in f:
            f.kill()
        self.assertIsInstance(f.wait().exit_code, int)
        with g.forward(OK, {"remote_port": 5432}) as fw:
            self.assertGreater(fw.listening[0]["local_port"], 0)
        self.assertEqual(g.agent_connect(OK), "agent session open on desk %s: screenshot 1x1" % OK)

    def test_async(self):
        async def go():
            a = AsyncGaiaDesk(backend="native", env={"PATH": os.environ.get("PATH", "")})
            r = await a.exec(OK, "exit 4")
            self.assertEqual(r["exit"], 4)
            s = await a.exec_stream(OK, "hostname")
            self.assertEqual((await s.wait()).exit_code, 0)

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
