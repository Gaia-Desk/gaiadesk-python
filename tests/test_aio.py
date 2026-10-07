"""The asyncio client against the fake gaiadesk-cli."""

import asyncio
import unittest

import helpers
from helpers import OFFLINE, OK, REFUSED

from gaiadesk import AsyncGaiaDesk, CliNotFoundError, McpError, OperationFailedError, RefusedError, UnreachableError, tool_text


class AsyncClientTest(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_exec_and_errors(self):
        async def go():
            gd, calls = helpers.setup(AsyncGaiaDesk, token_file="/t/bot.token")
            r = await gd.exec(OK, "hostname", stdin="x")
            self.assertEqual(r["exit"], 0)
            self.assertIn("stdin: x", r["stdout"])
            self.assertEqual(calls()[0]["env"], {"GAIADESK_TOKEN_FILE": "/t/bot.token"})
            with self.assertRaises(UnreachableError):
                await gd.exec(OFFLINE, "x")
            with self.assertRaises(RefusedError):
                await gd.exec(REFUSED, "x")
            r = await gd.shell(OK, "echo hi\n")
            self.assertEqual(r["stdout"], "ran: script:echo hi\n")

        self.run_async(go())

    def test_ops(self):
        async def go():
            gd, calls = helpers.setup(AsyncGaiaDesk, code="pw")
            self.assertEqual(await gd.version(), "gaiadesk-cli 0.10.324")
            self.assertEqual(len((await gd.devices())["devices"]), 2)
            self.assertEqual((await gd.upload("dist", OK, "x/", recursive=True))["dirs"], 1)
            self.assertEqual((await gd.run_job(OK, "build", "make"))["name"], "build")
            self.assertEqual(len(await gd.jobs(OK)), 2)
            self.assertEqual(await gd.job_logs(OK, "build"), "line1\nline2\n")
            with self.assertRaises(OperationFailedError):
                await gd.kill_job(OK, "nope")
            self.assertEqual((await gd.stats(OK))["cpus"], 8)
            self.assertEqual((await gd.list_tokens(OK))[0]["label"], "bot")
            self.assertEqual(await gd.mesh_ip(OK), "100.64.0.2")
            w = await gd.wait_job(OK, "failing")
            self.assertEqual((w["timed_out"], w["job"]["exit_code"]), (False, 3))
            self.assertTrue((await gd.wait_job(OK, "slow", timeout="1m"))["timed_out"])
            self.assertEqual((await gd.whoami())["source"], "none")
            self.assertEqual((await gd.run_job(OK, "build", "make", shell="zsh", env={"A": "1"}))["name"], "build")
            self.assertEqual((await gd.exec(OK, "x", env={"A": "1"}))["exit"], 0)
            s = await gd.exec_stream(OK, "x", env={"A": "a\nb"})
            self.assertEqual((await s.wait()).exit_code, 0)
            envd = [c for c in calls() if "--env" in c["argv"]]
            self.assertEqual([c["passed_env"] for c in envd], [{"A": "1"}, {"A": "1"}, {"A": "a\nb"}])
            self.assertFalse(any("=" in a for c in envd for a in c["argv"][c["argv"].index("--env"):c["argv"].index("--")]))
            # Concurrency: several commands at once.
            rs = await asyncio.gather(*(gd.exec(OK, "exit %d" % i) for i in range(4)))
            self.assertEqual([r["exit"] for r in rs], [0, 1, 2, 3])

        self.run_async(go())

    def test_streams_and_forward(self):
        async def go():
            gd, _ = helpers.setup(AsyncGaiaDesk)
            s = await gd.exec_stream(OK, "exit 2")
            out = ""
            async for name, t in s.text():
                if name == "stdout":
                    out += t
            self.assertEqual(out, "part1 part2 exit 2\n")
            self.assertEqual((await s.wait()).exit_code, 2)
            f = await gd.follow_job_logs(OK, "build")
            lines = "".join([t async for name, t in f.text() if name == "stdout"])
            self.assertEqual(lines, "one\ntwo\nthree\n")
            async with await gd.forward(OK, {"remote_port": 5432, "local_port": 15432}) as fw:
                self.assertEqual(fw.listening[0]["local_port"], 15432)
            with self.assertRaises(RefusedError):
                await gd.forward(REFUSED, {"remote_port": 22})

        self.run_async(go())

    def test_mcp(self):
        async def go():
            gd, _ = helpers.setup(AsyncGaiaDesk)
            async with await gd.mcp(audit_dir="/tmp/a") as m:
                tools, r = await asyncio.gather(m.list_tools(), m.call_tool("gaiadesk_exec", {"desk_id": OK, "command": "hostname"}))
                self.assertEqual(len(tools), 2)
                self.assertEqual(tool_text(r), "exit 0")
                with self.assertRaises(McpError):
                    await m.call_tool("gaiadesk_nope")

        self.run_async(go())

    def test_mcp_concurrent_calls(self):
        async def go():
            gd, _ = helpers.setup(AsyncGaiaDesk)
            async with await gd.mcp() as m:
                a, b = await asyncio.gather(
                    m.call_tool("gaiadesk_exec", {"desk_id": OK, "command": "hostname"}),
                    m.call_tool("gaiadesk_exec", {"desk_id": OK, "command": "hostname"}),
                )
                self.assertEqual((tool_text(a), tool_text(b)), ("exit 0", "exit 0"))

        self.run_async(go())

    def test_missing_cli(self):
        async def go():
            with self.assertRaises(CliNotFoundError):
                await AsyncGaiaDesk(cli="/nonexistent/gaiadesk-cli", env={}).version()

        self.run_async(go())


if __name__ == "__main__":
    unittest.main()
