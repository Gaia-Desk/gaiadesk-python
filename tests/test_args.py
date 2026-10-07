"""The argument vectors, checked against ``gaiadesk-cli <cmd> --help``."""

import unittest

import helpers  # noqa: F401  (puts src/ on sys.path)

from gaiadesk import UsageError, locate_cli
from gaiadesk import _args as A
from gaiadesk._core import standard_locations


class ArgsTest(unittest.TestCase):
    def test_exec(self):
        self.assertEqual(A.exec_args("123456789", "ls | wc -l", stdin=False, json=True),
                         ["exec", "--desk-id", "123456789", "--quiet", "--json", "--no-stdin", "--", "ls | wc -l"])
        self.assertEqual(A.exec_args("1", ["printf", "%s", "a b"], stdin=True, json=False, shell="none"),
                         ["exec", "--desk-id", "1", "--quiet", "--stdin", "--shell", "none", "--", "printf", "%s", "a b"])

    def test_exec_cwd_and_json_stream(self):
        self.assertEqual(A.exec_args("1", "make", stdin=False, json=False, json_stream=True, cwd="/srv/app", shell="sh"),
                         ["exec", "--desk-id", "1", "--quiet", "--json-stream", "--no-stdin", "--cwd", "/srv/app", "--shell", "sh", "--", "make"])
        with self.assertRaises(UsageError):
            A.exec_args("1", "x", stdin=False, json=True, json_stream=True)
        for bad in ("", " ", 3):
            with self.assertRaises(UsageError):
                A.exec_args("1", "x", stdin=False, json=True, cwd=bad)

    def test_env(self):
        self.assertEqual(A.exec_args("1", "make", stdin=False, json=True, env={"CI": "1", "MSG": "a b=c"}, shell="bash"),
                         ["exec", "--desk-id", "1", "--quiet", "--json", "--no-stdin", "--env", "CI", "--env", "MSG",
                          "--shell", "bash", "--", "make"], "names only: the values travel in gaiadesk-cli's environment")
        self.assertEqual(A.cli_env({"CI": "1", "MSG": "a\nb"}), {"CI": "1", "MSG": "a\nb"})
        self.assertIsNone(A.cli_env(None))
        self.assertEqual(A.exec_args("1", "x", stdin=False, json=True, env={}), A.exec_args("1", "x", stdin=False, json=True))
        for bad in ({"": "x"}, {"A=B": "x"}, {"A B": "x"}, {"A": 1}, {"A": "x\0y"}, ["A=1"]):
            with self.assertRaises(UsageError) as cm:
                A.exec_args("1", "x", stdin=False, json=True, env=bad)
            self.assertNotIn("x\0y", str(cm.exception), "an error never shows a value")
        for s in ("bash", "zsh"):
            self.assertIn(s, A.exec_args("1", "x", stdin=False, json=True, shell=s))

    def test_env_names_the_cli_itself_reads_stay_on_argv(self):
        env = {"GAIADESK_TOKEN": "t", "gaiadesk_x": "y", "PATH": "/opt/bin", "HOME": "/h", "LC_ALL": "C", "Path": "p", "CI": "1"}
        self.assertEqual(A.env_flags(env, "darwin"),
                         ["--env", "GAIADESK_TOKEN=t", "--env", "gaiadesk_x=y", "--env", "PATH=/opt/bin", "--env", "HOME=/h",
                          "--env", "LC_ALL=C", "--env", "Path", "--env", "CI"])
        self.assertEqual(A.cli_env(env, "darwin"), {"Path": "p", "CI": "1"})
        self.assertEqual(A.cli_env(env, "win32"), {"CI": "1"}, "case-insensitive on Windows")
        self.assertIn("systemroot=x", A.env_flags({"systemroot": "x"}, "win32"))
        self.assertIsNone(A.cli_env({"SystemRoot": "x", "ComSpec": "y", "TMPDIR": "z", "TEMP": "a", "TMP": "b", "USERPROFILE": "c", "LANG": "d"}, "linux"))

    def test_wait_and_whoami(self):
        self.assertEqual(A.wait_args("1", "build"), ["wait", "build", "--desk-id", "1", "--json"])
        self.assertEqual(A.wait_args("1", "build", timeout="10m"), ["wait", "build", "--desk-id", "1", "--timeout", "10m", "--json"])
        self.assertEqual(A.wait_args("1", "build", timeout=1.5)[4:6], ["--timeout", "2"])
        for bad in (dict(name="-x"), dict(timeout=-1)):
            with self.assertRaises(UsageError):
                A.wait_args("1", **dict(dict(name="b"), **bad))
        self.assertEqual(A.whoami_args(), ["whoami", "--json"])

    def test_durations(self):
        a = A.exec_args("1", "x", stdin=False, json=True, timeout=1.2, connect_timeout="90s", persist=0, verbose=True)
        self.assertEqual(a[6:-2], ["--timeout", "2", "--connect-timeout", "90s", "--persist", "0", "--verbose"])
        for bad in (dict(connect_timeout=0), dict(timeout=-1), dict(timeout="1 day; rm"), dict(timeout=True)):
            with self.assertRaises(UsageError):
                A.exec_args("1", "x", stdin=False, json=True, **bad)

    def test_bad_input(self):
        for desk, cmd, kw in (("", "x", {}), ("39 2586273", "x", {}), ("--code", "x", {}), ("1", "", {}), ("1", [], {}), ("1", "x", {"shell": "fish"})):
            with self.assertRaises(UsageError):
                A.exec_args(desk, cmd, stdin=False, json=True, **kw)

    def test_no_credentials_in_argv(self):
        all_args = (A.exec_args("1", "x", stdin=False, json=True) + A.shell_args("1", json=True) + A.cp_args("upload", "1", "a", "b", False)
                    + A.token_create_args("1") + A.devices_args(True, "1"))
        for f in ("--code", "--allow-code-in-argv", "--token-file", "--token"):
            self.assertNotIn(f, all_args)

    def test_cp(self):
        self.assertEqual(A.cp_args("upload", "123456789", "report.pdf", "Documents/", False), ["cp", "--json", "report.pdf", "123456789:Documents/"])
        self.assertEqual(A.cp_args("download", "123456789", "./app.log", "logs/app.log", True),
                         ["cp", "--recursive", "--json", "123456789:logs/app.log", "./app.log"])
        self.assertEqual(A.local_path("build:out/x"), "./build:out/x")
        self.assertEqual(A.local_path("-weird"), "./-weird")
        self.assertEqual(A.local_path("C:\\Users\\me\\a.txt"), "C:\\Users\\me\\a.txt")

    def test_run(self):
        self.assertEqual(A.run_args("234567890", "build", "msbuild app.sln /m", priority="low", cpu=50, mem="4G", keep_awake=True),
                         ["run", "--detach", "--name", "build", "--desk-id", "234567890", "--priority", "low", "--cpu", "50", "--mem", "4G",
                          "--keep-awake", "--json", "--", "msbuild app.sln /m"])
        self.assertIn("--no-keep-awake", A.run_args("1", "b", ["./build.sh"], keep_awake=False))
        self.assertEqual(A.run_args("1", "b", "make", shell="pwsh", env={"CONFIG": "Release"}),
                         ["run", "--detach", "--name", "b", "--desk-id", "1", "--shell", "pwsh", "--env", "CONFIG", "--json", "--", "make"])
        for bad in ("none", "default", "fish"):
            with self.assertRaises(UsageError):
                A.run_args("1", "b", "make", shell=bad)
        self.assertEqual(A.run_args("1", "b", "make", cwd="C:\\src"), ["run", "--detach", "--name", "b", "--desk-id", "1", "--cwd", "C:\\src", "--json", "--", "make"])
        for kw in (dict(name="-x"), dict(cpu=0), dict(priority="urgent")):
            name = kw.pop("name", "b")
            with self.assertRaises(UsageError):
                A.run_args("1", name, "make", **kw)

    def test_jobs_stats_measure(self):
        self.assertEqual(A.ps_args("1"), ["ps", "--desk-id", "1", "--json"])
        self.assertEqual(A.kill_args("1", "build"), ["kill", "build", "--desk-id", "1", "--json"])
        self.assertEqual(A.logs_args("1", "build", 100, True), ["logs", "build", "--desk-id", "1", "--follow", "--json", "--tail", "100"])
        self.assertEqual(A.stats_args("1"), ["stats", "--desk-id", "1", "--json"])
        self.assertEqual(A.measure_args("1", 5), ["measure", "--desk-id", "1", "--count", "5", "--json"])
        with self.assertRaises(UsageError):
            A.measure_args("1", 0)

    def test_tokens_audit(self):
        self.assertEqual(A.token_create_args(["1", "2"], name="bot", expires="3d", scopes=["exec", "cp"], cwd="/srv", low_priv=True, out="/tmp/t"),
                         ["token", "create", "--desk", "1,2", "--name", "bot", "--expires", "3d", "--scope", "exec,cp", "--cwd", "/srv",
                          "--low-priv", "--out", "/tmp/t", "--json"])
        self.assertEqual(A.token_revoke_args("1", "bot", False, False), ["token", "revoke", "--desk", "1", "bot", "--json"])
        self.assertEqual(A.token_revoke_args("1", None, True, True), ["token", "revoke", "--desk", "1", "--all-for-desk", "--account", "--json"])
        with self.assertRaises(UsageError):
            A.token_revoke_args("1", None, False, False)
        self.assertEqual(A.audit_args("1", "bot", 200, True), ["audit", "--desk", "1", "--token", "bot", "--limit", "200", "--account", "--json"])

    def test_forward_mcp_misc(self):
        self.assertEqual(A.forward_args("1", [{"remote_port": 5432, "local_port": 15432}, {"remote_port": 80, "remote_host": "db.lan"}]),
                         ["forward", "--json", "1:5432", "localhost:15432", "1:db.lan:80", "localhost:0"])
        with self.assertRaises(UsageError):
            A.forward_args("1", [{"remote_port": 0}])
        self.assertEqual(A.mcp_args("/a", ["example.com"], "wss://x/ws"), ["mcp", "--server", "wss://x/ws", "--allow-domain", "example.com", "--audit-dir", "/a"])
        self.assertEqual(A.disconnect_args(), ["disconnect", "--all", "--json"])
        self.assertEqual(A.agent_connect_args("1", "wss://x/ws"), ["agent-connect", "--desk-id", "1", "--server", "wss://x/ws", "--json"])

    def test_locate(self):
        self.assertEqual(locate_cli({"GAIADESK_CLI": "/x/cli"}, "linux", lambda p: False), "/x/cli")
        self.assertEqual(locate_cli({"PATH": "/a:/b"}, "linux", lambda p: p == "/b/gaiadesk-cli"), "/b/gaiadesk-cli")
        self.assertEqual(locate_cli({"PATH": "/a"}, "darwin", lambda p: p.startswith("/Applications/")),
                         "/Applications/GaiaDesk.app/Contents/MacOS/gaiadesk-cli")
        self.assertEqual(locate_cli({"ProgramFiles": "C:\\Program Files"}, "win32", lambda p: "Program Files" in p),
                         "C:\\Program Files\\GaiaDesk\\gaiadesk-cli.exe")
        self.assertEqual(locate_cli({}, "linux", lambda p: False), "gaiadesk-cli")
        self.assertEqual(standard_locations("linux", {}, "/home/me")[-1], "/home/me/.local/bin/gaiadesk-cli")


if __name__ == "__main__":
    unittest.main()
