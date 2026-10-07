"""The sync client against the fake gaiadesk-cli (fixtures/fake_cli.py)."""

import os
import sys
import tempfile
import unittest

import helpers
from helpers import OFFLINE, OK, PLAIN, REFUSED, USAGE

from gaiadesk import (
    CliNotFoundError,
    CommandError,
    GaiaDesk,
    GaiaDeskError,
    McpError,
    OperationFailedError,
    ProtocolError,
    RefusedError,
    UnreachableError,
    UsageError,
    tool_image,
    tool_text,
)


def text_of(stream):
    out, err = "", ""
    for name, t in stream.text():
        if name == "stdout":
            out += t
        else:
            err += t
    return out, err


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.gd, self.calls = helpers.setup(GaiaDesk)

    def test_version(self):
        self.assertEqual(self.gd.version(), "gaiadesk-cli 0.10.324")

    def test_credentials_in_env_never_argv(self):
        gd, calls = helpers.setup(GaiaDesk, token_file="/t/bot.token", account_token="acct", agent_token="gdagt_x",
                                  server="wss://example.invalid/ws", persist=30)
        gd.exec(OK, "hostname")
        c = calls()[0]
        self.assertEqual(c["env"], {"GAIADESK_TOKEN_FILE": "/t/bot.token", "GAIADESK_TOKEN": "acct", "GAIADESK_AGENT_TOKEN": "gdagt_x",
                                    "GAIADESK_SERVER": "wss://example.invalid/ws", "GAIADESK_PERSIST": "30"})
        self.assertFalse(any("bot.token" in a or "acct" in a for a in c["argv"]))

    def test_explicit_code_beats_inherited_token_file(self):
        log = os.path.join(tempfile.mkdtemp(), "calls.jsonl")
        env = helpers.base_env(log)
        env["GAIADESK_TOKEN_FILE"] = "/inherited"
        gd = GaiaDesk(cli=[sys.executable, helpers.FAKE], env=env, code="pw")
        gd.exec(OK, "x")
        import json

        with open(log) as f:
            self.assertEqual(json.loads(f.read())["env"], {"GAIADESK_CODE": "pw"})

    def test_exec(self):
        r = self.gd.exec(OK, "hostname", shell="sh", timeout=10)
        self.assertEqual((r["exit"], r["remote_code"], r["stdout"], r["stderr"], r["route"]), (0, 0, "ran: hostname\n", "warn\n", "LAN"))
        self.assertEqual(self.calls()[0]["argv"], ["exec", "--desk-id", OK, "--quiet", "--json", "--no-stdin", "--shell", "sh", "--timeout", "10", "--", "hostname"])

    def test_exec_stdin(self):
        r = self.gd.exec(OK, ["wc", "-l"], stdin="a\nb\n")
        self.assertIn("stdin: a\nb\n", r["stdout"])
        self.assertIn("--stdin", self.calls()[0]["argv"])

    def test_exec_nonzero_and_check(self):
        self.assertEqual(self.gd.exec(OK, "exit 3")["exit"], 3)
        with self.assertRaises(CommandError) as cm:
            self.gd.exec(OK, "exit 3", check=True)
        self.assertEqual(cm.exception.result["exit"], 3)
        t = self.gd.exec(OK, "sleep")
        self.assertEqual((t["timed_out"], t["exit"]), (True, 124))

    def test_exec_failures_are_typed(self):
        with self.assertRaises(UnreachableError) as cm:
            self.gd.exec(OFFLINE, "x")
        self.assertEqual((cm.exception.kind, cm.exception.exit_code), ("offline", 255))
        with self.assertRaises(UsageError):
            self.gd.exec(USAGE, "x")
        with self.assertRaises(RefusedError) as cm:
            self.gd.exec(REFUSED, "x")
        self.assertIn("`exec` scope", str(cm.exception))
        with self.assertRaises(GaiaDeskError) as cm:
            self.gd.exec(PLAIN, "x")
        self.assertEqual(str(cm.exception), "something odd")

    def test_missing_cli(self):
        with self.assertRaises(CliNotFoundError) as cm:
            GaiaDesk(cli="/nonexistent/gaiadesk-cli", env={}).version()
        self.assertIn("https://gaiadesk.net/download", str(cm.exception))

    def test_exec_stream(self):
        s = self.gd.exec_stream(OK, "exit 2")
        out, err = text_of(s)
        self.assertEqual((out, err), ("part1 part2 exit 2\n", "warn\n"))
        self.assertEqual(s.wait().exit_code, 2)

    def test_exec_stream_open_stdin(self):
        s = self.gd.exec_stream(OK, "cat", stdin=True)
        s.write("hello")
        s.end()
        out, _ = text_of(s)
        self.assertIn("stdin: hello", out)
        self.assertEqual(s.wait().exit_code, 0)

    def test_shell(self):
        r = self.gd.shell(OK, "cd /tmp\nls\n", shell="sh")
        self.assertEqual(r["stdout"], "ran: script:cd /tmp\nls\n")
        c = self.calls()[0]
        self.assertEqual(c["argv"], ["shell", "--desk-id", OK, "--quiet", "--json", "--shell", "sh"])
        self.assertEqual(c["stdin"], "cd /tmp\nls\n")

    def test_devices_and_probe(self):
        self.assertEqual(len(self.gd.devices()["devices"]), 2)
        self.assertEqual([d["reachable"] for d in self.gd.devices(probe=True)["devices"]], [True, False])
        self.assertEqual(self.gd.probe(OK)["probe"]["route"], "LAN")

    def test_cp(self):
        up = self.gd.upload("dist", OK, "deploy/", recursive=True)
        self.assertEqual((up["direction"], up["dirs"]), ("upload", 1))
        self.assertEqual(self.calls()[0]["argv"], ["cp", "--recursive", "--json", "dist", OK + ":deploy/"])
        self.assertEqual(self.gd.download(OK, "logs/app.log", "./app.log")["direction"], "download")
        with self.assertRaises(OperationFailedError) as cm:
            self.gd.upload("fail.txt", OK, "x/")
        self.assertEqual(len(cm.exception.json["failed"]), 1)
        with self.assertRaises(RefusedError):
            self.gd.upload("a", REFUSED, "x/")
        with self.assertRaises(GaiaDeskError) as cm:
            self.gd.upload("a", PLAIN, "x/")
        self.assertIn("offline", str(cm.exception))

    def test_jobs(self):
        self.assertEqual(self.gd.run_job(OK, "build", ["make", "-j8"], priority="low", cpu=50)["state"], "running")
        with self.assertRaises(RefusedError):
            self.gd.run_job(REFUSED, "build", "make")
        self.assertEqual([j["state"] for j in self.gd.jobs(OK)], ["running", "exited"])
        self.assertEqual(self.gd.job_logs(OK, "build"), "line1\nline2\n")
        self.assertEqual(self.gd.job_logs(OK, "build", tail=10), "tail\n")
        with self.assertRaises(OperationFailedError) as cm:
            self.gd.job_logs(OK, "nope")
        self.assertEqual(str(cm.exception), "no job named nope")
        self.assertEqual(self.gd.kill_job(OK, "build")["state"], "killed")
        with self.assertRaises(OperationFailedError):
            self.gd.kill_job(OK, "nope")

    def test_follow_logs(self):
        s = self.gd.follow_job_logs(OK, "build")
        out, _ = text_of(s)
        self.assertEqual(out, "one\ntwo\nthree\n")
        self.assertEqual(s.wait().stderr_tail, "job build exited (exit 0)")

    def test_stats_measure(self):
        self.assertEqual(self.gd.stats(OK)["cpus"], 8)
        with self.assertRaises(GaiaDeskError) as cm:
            self.gd.stats(PLAIN)
        self.assertEqual(str(cm.exception), "the desk did not answer")
        self.assertEqual(self.gd.measure(OK, count=5)["sent"], 5)
        self.assertIsNone(self.gd.measure(REFUSED)["rtt_ms"])

    def test_tokens(self):
        with self.assertRaises(RefusedError):
            self.gd.list_tokens(OK)
        gd, calls = helpers.setup(GaiaDesk, code="owner-pw")
        made = gd.create_token([OK, OFFLINE], name="bot", scopes=["exec", "cp"])
        self.assertEqual(len(made["tokens"]), 2)
        self.assertTrue(made["tokens"][0]["secret"].startswith("gdagt_"))
        to_file = gd.create_token(OK, out="/tmp/bot.token")
        self.assertEqual(to_file["file"], "/tmp/bot.token")
        self.assertNotIn("secret", to_file["tokens"][0])
        self.assertEqual(calls()[0]["env"]["GAIADESK_CODE"], "owner-pw")
        self.assertEqual(gd.list_tokens(OK)[0]["id"], "9f3a1c2b7d004e11")
        self.assertEqual(gd.revoke_token(OK, "bot"), {"revoked": "bot", "stopped_sessions": 1})
        self.assertEqual(gd.revoke_token(OK, all_for_desk=True)["revoked"], "bot")
        self.assertTrue(gd.revoke_token(OK, "bot", account=True)["ok"])
        with self.assertRaises(OperationFailedError):
            gd.revoke_token(OK, "ghost")
        self.assertEqual(gd.audit(OK, token="bot")[0]["action"], "exec.end")

    def test_mesh_disconnect(self):
        self.assertEqual(self.gd.mesh_status()["peers"][0]["mesh_ip"], "100.64.0.2")
        self.assertEqual(self.gd.mesh_ip(OK), "100.64.0.2")
        with self.assertRaises(OperationFailedError):
            self.gd.mesh_ip(OFFLINE)
        self.gd.disconnect(OK)
        self.gd.disconnect()
        self.assertEqual([c["argv"] for c in self.calls()][-2:], [["disconnect", "--desk-id", OK], ["disconnect", "--all"]])

    def test_forward(self):
        with self.gd.forward(OK, [{"remote_port": 5432, "local_port": 15432}, {"remote_port": 80, "remote_host": "db.lan"}]) as f:
            self.assertEqual([(l["local_port"], l["remote_host"], l["remote_port"]) for l in f.listening],
                             [(15432, "127.0.0.1", 5432), (40002, "db.lan", 80)])
        with self.assertRaises(RefusedError) as cm:
            self.gd.forward(REFUSED, {"remote_port": 22})
        self.assertIn("refused the forward", str(cm.exception))

    def test_agent_connect(self):
        with self.assertRaises(GaiaDeskError):
            self.gd.agent_connect(OK)
        gd, _ = helpers.setup(GaiaDesk, agent_token="gdagt_x")
        self.assertIn("screenshot 1280x800", gd.agent_connect(OK))

    def test_mcp(self):
        gd, _ = helpers.setup(GaiaDesk, server="wss://example.invalid/ws")
        with gd.mcp(audit_dir="/tmp/audit") as m:
            self.assertEqual([t["name"] for t in m.list_tools()], ["gaiadesk_exec", "gaiadesk_screenshot"])
            r = m.call_tool("gaiadesk_exec", {"desk_id": OK, "command": "hostname"})
            self.assertFalse(r["isError"])
            self.assertEqual(r["structuredContent"]["stdout"], "ran: hostname\n")
            self.assertEqual(tool_text(r), "exit 0")
            self.assertEqual(tool_image(m.call_tool("gaiadesk_screenshot", {"session_id": "h"})), {"mime_type": "image/png", "base64": "iVBORw0K"})
            with self.assertRaises(McpError) as cm:
                m.call_tool("gaiadesk_nope")
            self.assertEqual(cm.exception.code, -32602)
            self.assertEqual(m.request("tools/list")["argv"], ["mcp", "--server", "wss://example.invalid/ws", "--audit-dir", "/tmp/audit"])
        with self.assertRaises(GaiaDeskError):
            m.list_tools()

    def test_mcp_tool_name_spellings(self):
        # A server before 0.10.324: dotted names.
        gd, _ = helpers.setup(GaiaDesk, old=True)
        with gd.mcp() as m:
            self.assertEqual([t["name"] for t in m.list_tools()], ["gaiadesk.exec", "gaiadesk.screenshot"])
            self.assertEqual(tool_text(m.call_tool("gaiadesk_exec", {"desk_id": OK, "command": "hostname"})), "exit 0")
        # 0.10.324+: gaiadesk_<tool>; the dotted spelling still resolves.
        gd2, _ = helpers.setup(GaiaDesk)
        with gd2.mcp() as m:
            self.assertEqual(tool_text(m.call_tool("gaiadesk.exec", {"desk_id": OK, "command": "hostname"})), "exit 0")
            self.assertEqual(tool_text(m.call_tool("gaiadesk_exec", {"desk_id": OK, "command": "hostname"})), "exit 0")
            self.assertEqual(tool_image(m.call_tool("gaiadesk.screenshot", {"session_id": "h"})), {"mime_type": "image/png", "base64": "iVBORw0K"})
            with self.assertRaises(McpError):
                m.call_tool("gaiadesk.nope")

    def test_protocol_error_and_raw(self):
        with self.assertRaises(ProtocolError):
            self.gd.jobs(PLAIN)
        done = self.gd.raw(["mesh", "ip", OK])
        self.assertEqual((done.code, done.stdout.strip()), (0, "100.64.0.2"))


if __name__ == "__main__":
    unittest.main()
