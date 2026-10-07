"""The native backend (gaiadesk_native) through a mock module
(fixtures/mock_native.py), and both backends side by side: the same calls
must give the same results, error classes and kinds."""

import asyncio
import os
import sys
import unittest

import helpers
from helpers import OK

sys.path.insert(0, os.path.join(helpers.HERE, "fixtures"))
from mock_native import LOST, OFFLINE, REFUSED, USAGE, make_mock  # noqa: E402

from gaiadesk._core import clear_feature_cache  # noqa: E402

from gaiadesk import (  # noqa: E402
    AsyncGaiaDesk,
    CliNotFoundError,
    CommandError,
    ConnectionLostError,
    GaiaDesk,
    GaiaDeskError,
    OperationFailedError,
    RefusedError,
    UnreachableError,
    UsageError,
)
from gaiadesk._native import load_native  # noqa: E402

BASE = {k: os.environ[k] for k in ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}


def native(cls=GaiaDesk, **opts):
    m, calls, clients = make_mock()
    opts.setdefault("env", dict(BASE))
    return cls(native=m, **opts), calls, clients


def cli(cls=GaiaDesk, **opts):
    return helpers.setup(cls, **opts)[0]


class Choosing(unittest.TestCase):
    def test_backend_selection(self):
        self.assertEqual(native()[0].backend, "native")
        self.assertEqual(native(backend="cli")[0].backend, "cli")
        self.assertEqual(native(cli="/x/gaiadesk-cli")[0].backend, "cli")
        self.assertEqual(native(cli="/x/gaiadesk-cli", backend="native")[0].backend, "native")
        self.assertEqual(native(env=dict(BASE, GAIADESK_SDK_BACKEND="cli"))[0].backend, "cli")
        with self.assertRaises(UsageError):
            native(backend="bogus")[0].backend

    def test_without_the_extension(self):
        def missing(name):
            raise ImportError("No module named 'gaiadesk_native'")

        self.assertEqual(load_native(missing)[0], None)
        gd = GaiaDesk(env=dict(BASE))
        if load_native()[0] is None:  # not installed in this environment
            self.assertEqual(gd.backend, "cli")
            with self.assertRaises(CliNotFoundError):
                GaiaDesk(backend="native", env=dict(BASE)).backend

    def test_a_binary_that_does_not_load_counts_as_absent(self):
        class Broken:
            NativeClient = object

            @staticmethod
            def build_info():
                raise OSError("no binary for this platform")

        mod, why = load_native(lambda name: Broken)
        self.assertIsNone(mod)
        self.assertIn("no binary", why)

    def test_credentials(self):
        gd, _, clients = native(token_file="/t/bot.token", account_token="acct", agent_token="gdagt_x",
                                server="wss://example.invalid/ws", persist=30, cwd="/w")
        gd.stats(OK)
        self.assertEqual(clients[0], {"token_file": "/t/bot.token", "account_token": "acct", "agent_token": "gdagt_x",
                                      "server": "wss://example.invalid/ws", "persist": "30", "cwd": "/w"})
        gd, _, clients = native(env=dict(BASE, GAIADESK_TOKEN_FILE="/inherited"), code="pw")
        gd.stats(OK)
        self.assertEqual(clients[0], {"code": "pw"})
        with self.assertRaises(UsageError):
            native(persist="forever")[0].backend


class Operations(unittest.TestCase):
    def test_exec_arguments(self):
        gd, calls, _ = native()
        r = gd.exec(OK, ["ls", "-l"], shell="sh", timeout=90, connect_timeout="30s", stdin="hi")
        self.assertEqual(r["stdout"], "ran: ls -l\nstdin: hi\n")
        self.assertEqual(calls[0], {"op": "exec", "args": {"desk_id": OK, "command": ["ls", "-l"], "shell": "sh", "timeout": "90",
                                                           "connect_timeout": "30s"}, "input": b"hi"})
        with self.assertRaises(CommandError) as c:
            gd.exec(OK, "exit 3", check=True)
        self.assertEqual(c.exception.result["exit"], 3)
        self.assertEqual(gd.shell(OK, "echo hi\n")["stdout"], "ran: script:echo hi\n")
        gd.shell(OK, "echo hi\n", cwd="/srv/app")
        self.assertEqual(calls[-1]["args"].get("cwd"), "/srv/app", "shell cwd reaches the native library")

    def test_every_other_operation(self):
        gd, calls, _ = native()
        gd.devices(probe=True)
        self.assertTrue(gd.probe(OK)["reachable"])
        gd.upload("./a", OK, "b/", recursive=True)
        gd.download(OK, "b", "./a")
        gd.run_job(OK, "build", "make", priority="low", cpu=50, mem="4G", keep_awake=True)
        gd.jobs(OK)
        self.assertEqual(gd.job_logs(OK, "build", tail=100), "line 1\nline 2\n")
        gd.kill_job(OK, "build")
        gd.stats(OK)
        gd.measure(OK, count=3)
        gd.create_token(OK, name="bot", scopes=["exec"], low_priv=True)
        gd.list_tokens(OK)
        gd.revoke_token(OK, all_for_desk=True)
        self.assertEqual(gd.revoke_token(OK, "bot", account=True), {"desk": OK, "ok": True, "message": "revoked"})
        gd.audit(OK, limit=5)
        gd.mesh_status()
        self.assertEqual(gd.mesh_ip(OK), "100.64.0.1")
        self.assertEqual(gd.disconnect(OK), {"closed": [OK]})
        self.assertEqual(gd.disconnect(), {"closed": []})
        self.assertEqual(gd.version(), "gaiadesk-native 0.10.324")
        by_op = {c["op"]: c["args"] for c in calls}
        self.assertEqual(by_op["job_run"], {"desk_id": OK, "name": "build", "command": "make",
                                            "limits": {"priority": "low", "cpu_percent": 50, "mem_mb": 4096, "keep_awake": True}})
        self.assertEqual(by_op["token_mint"], {"desks": [OK], "name": "bot", "scopes": ["exec"], "low_priv": True})
        self.assertEqual(by_op["token_revoke"]["account"], True)
        self.assertEqual(by_op["job_logs"], {"desk_id": OK, "name": "build", "tail": 100})

    def test_wait_whoami_env_shell(self):
        gd, calls, _ = native()
        r = gd.wait_job(OK, "build")
        self.assertEqual((r["timed_out"], r["job"]["exit_code"]), (False, 0))
        self.assertTrue(gd.wait_job(OK, "slow", timeout=30)["timed_out"])
        with self.assertRaises(OperationFailedError):
            gd.wait_job(OK, "nope")
        self.assertEqual(gd.whoami(), {"source": "app", "account": "you@example.com"})
        gd.exec(OK, "make", env={"CI": "1"})
        gd.run_job(OK, "build", "make", shell="bash", env={"JOBS": "8"})
        s = gd.exec_stream(OK, "make", env={"CI": "1"})
        self.assertEqual(s.wait().exit_code, 0)
        waits = [c["args"] for c in calls if c["op"] == "job_wait"]
        self.assertEqual(waits[:2], [{"desk_id": OK, "name": "build"}, {"desk_id": OK, "name": "slow", "timeout": "30"}])
        by_op = {c["op"]: c["args"] for c in calls}
        self.assertEqual(by_op["whoami"], {})
        self.assertEqual(by_op["exec"]["env"], {"CI": "1"})
        self.assertEqual(by_op["job_run"], {"desk_id": OK, "name": "build", "command": "make", "limits": {}, "shell": "bash", "env": {"JOBS": "8"}})
        self.assertEqual(by_op["stream:exec"]["env"], {"CI": "1"})
        with self.assertRaises(UsageError):
            gd.exec(OK, "make", env={"A B": "x"})

    def test_cwd(self):
        gd, calls, _ = native()
        self.assertIn("cwd: /srv/app", gd.exec(OK, "make", cwd="/srv/app")["stdout"])
        gd.run_job(OK, "build", "make", cwd="src")
        s = gd.exec_stream(OK, "make", cwd="/w")
        self.assertEqual(s.wait().exit_code, 0)
        self.assertEqual(s.result["exit"], 0)
        by_op = {c["op"]: c["args"] for c in calls}
        self.assertEqual(by_op["exec"]["cwd"], "/srv/app")
        self.assertEqual(by_op["job_run"], {"desk_id": OK, "name": "build", "command": "make", "limits": {}, "cwd": "src"})
        self.assertEqual(by_op["stream:exec"]["cwd"], "/w")
        with self.assertRaises(UsageError):
            gd.exec(OK, "make", cwd="")

    def test_result_shapes(self):
        gd = native()[0]
        self.assertEqual([j["name"] for j in gd.jobs(OK)], ["build"])
        self.assertEqual(gd.list_tokens(OK)[0]["label"], "bot")
        self.assertEqual(gd.audit(OK)[0]["action"], "exec.end")
        self.assertEqual(gd.job_logs(OK, "build"), "line 1\nline 2\n")
        self.assertEqual(gd.mesh_ip(OK), "100.64.0.1")
        self.assertEqual(gd.disconnect(OK), {"closed": [OK]})

    def test_errors_carry_the_envelope(self):
        gd = native()[0]
        with self.assertRaises(UnreachableError) as c:
            gd.exec(OFFLINE, "x")
        e = c.exception
        self.assertEqual((e.kind, e.reason, e.desk, e.exit_code), ("offline", "offline", OFFLINE, 255))
        self.assertEqual(e.json["error"]["kind"], "unreachable")
        with self.assertRaises(OperationFailedError) as c:
            gd.kill_job(OK, "nope")
        self.assertEqual((c.exception.kind, c.exception.desk), ("failed", OK))

    def test_streams(self):
        gd, _, _ = native()
        s = gd.exec_stream(OK, "make")
        out = "".join(t for name, t in s.text() if name == "stdout")
        self.assertEqual(out, "part1 part2 make\n")
        e = s.wait()
        self.assertEqual((e.exit_code, e.stderr_tail), (0, "warn"))
        sh = gd.shell_stream(OK)
        sh.write("ls\n")
        sh.end()
        self.assertEqual(b"".join(c.data for c in sh), b"stdin: ls\n")
        f = gd.follow_job_logs(OK, "forever")
        for c in f:
            f.kill()
        e = f.wait()
        self.assertEqual((e.exit_code, e.stderr_tail), (130, "interrupted"))
        with self.assertRaises(RefusedError):
            gd.exec_stream(REFUSED, "x")

    def test_forward_and_agent_connect(self):
        gd, _, _ = native()
        with gd.forward(OK, [{"remote_port": 5432}, {"remote_port": 80, "remote_host": "printer", "local_port": 8080}]) as f:
            self.assertEqual([l["local_port"] for l in f.listening], [54321, 8080])
        self.assertEqual(f.close().exit_code, 0)
        with self.assertRaises(RefusedError):
            gd.forward(REFUSED, {"remote_port": 1})
        self.assertEqual(gd.agent_connect(OK), "agent session open on desk %s: screenshot 1280x800" % OK)

    def test_async(self):
        async def go():
            gd, _, _ = native(AsyncGaiaDesk)
            self.assertEqual((await gd.exec(OK, "exit 2"))["exit"], 2)
            s = await gd.exec_stream(OK, "make")
            chunks = [c async for c in s]
            self.assertEqual(chunks[0].data, b"part1 part2 make\n")
            self.assertEqual((await s.wait()).exit_code, 0)
            with self.assertRaises(UnreachableError) as c:
                await gd.stats(OFFLINE)
            self.assertEqual(c.exception.kind, "offline")
            f = await gd.forward(OK, {"remote_port": 1})
            self.assertEqual((await f.close()).exit_code, 0)
            self.assertIn("1280x800", await gd.agent_connect(OK))

        asyncio.run(go())


SCENARIOS = [
    ("exec on an offline desk", lambda gd: gd.exec(OFFLINE, "x"), UnreachableError, "offline"),
    ("exec refused", lambda gd: gd.exec(REFUSED, "x"), RefusedError, "refused"),
    ("exec with no credential", lambda gd: gd.exec(USAGE, "x"), UsageError, "usage"),
    ("a bad desk id", lambda gd: gd.exec("1 2", "x"), UsageError, "usage"),
    ("an empty command", lambda gd: gd.exec(OK, ""), UsageError, "usage"),
    ("check=True on a non-zero exit", lambda gd: gd.exec(OK, "exit 2", check=True), CommandError, None),
    ("a copy with a failed file", lambda gd: gd.upload("./fail.txt", OK, "x/"), OperationFailedError, "failed"),
    ("a bad job name", lambda gd: gd.kill_job(OK, "-x"), UsageError, "usage"),
]


class BothBackends(unittest.TestCase):
    def setUp(self):
        clear_feature_cache()

    def test_cwd_on_both(self):
        for gd in (cli(GaiaDesk), native()[0]):
            self.assertIn("cwd: /srv", gd.exec(OK, "make", cwd="/srv")["stdout"])
            self.assertEqual(gd.run_job(OK, "b", "make", cwd="/srv")["state"], "running")

    def test_same_errors(self):
        for name, run, cls, kind in SCENARIOS:
            for backend, gd in (("cli", cli(GaiaDesk)), ("native", native()[0])):
                with self.subTest(name=name, backend=backend):
                    with self.assertRaises(cls) as c:
                        run(gd)
                    if kind is not None:
                        self.assertEqual(c.exception.kind, kind)

    def test_a_failed_copy_carries_the_summary(self):
        for gd in (cli(GaiaDesk), native()[0]):
            with self.assertRaises(OperationFailedError) as c:
                gd.upload("./fail.txt", OK, "x/")
            self.assertEqual(len(c.exception.json["failed"]), 1)

    def test_lost_connection(self):
        with self.assertRaises(ConnectionLostError) as c:
            native()[0].stats(LOST)
        self.assertEqual((c.exception.kind, c.exception.exit_code), ("connection_lost", 253))

    def test_same_results(self):
        for gd in (cli(GaiaDesk, code="pw"), native(code="pw")[0]):
            r = gd.exec(OK, "exit 3")
            self.assertEqual((r["exit"], r["desk"]), (3, OK))
            self.assertEqual(gd.run_job(OK, "build", "make")["state"], "running")
            self.assertTrue(gd.revoke_token(OK, "bot", account=True)["ok"])

    def test_errors_are_gaiadesk_errors(self):
        with self.assertRaises(GaiaDeskError):
            native()[0].kill_job(OK, "nope")


if __name__ == "__main__":
    unittest.main()
