"""The same behaviour on both transports: every case in ``Shared`` runs once
against the CLI transport (the fake gaiadesk-cli) and once against the API
transport (the mock hosted API, which answers from the same fake CLI).
Results, error classes, kinds, reasons, desks and exit codes must match."""

import os
import sys
import tempfile
import unittest

import helpers
from helpers import OFFLINE, OK, PLAIN, REFUSED, USAGE

sys.path.insert(0, os.path.join(helpers.HERE, "fixtures"))
from mock_api import MockApi  # noqa: E402

from gaiadesk import (  # noqa: E402
    CommandError,
    GaiaDesk,
    GaiaDeskError,
    OperationFailedError,
    ProtocolError,
    RefusedError,
    UnreachableError,
    UsageError,
)

API = None


def setUpModule():
    global API
    API = MockApi()


def tearDownModule():
    API.close()


def text_of(stream):
    out, err = "", ""
    for name, t in stream.text():
        if name == "stdout":
            out += t
        else:
            err += t
    return out, err


def local_files():
    d = tempfile.mkdtemp(prefix="gaiadesk-sdk-files-")
    for name, body in (("app.txt", "hello desk\n"), ("fail.txt", "nope\n")):
        with open(os.path.join(d, name), "w") as f:
            f.write(body)
    return d


class Shared:
    """The cases. Subclasses say how to make a client."""

    name = ""

    def desk(self):
        raise NotImplementedError

    def owner(self):
        raise NotImplementedError

    def anon(self):
        raise NotImplementedError

    def test_backend(self):
        self.assertEqual(self.desk().backend, self.name)

    # exec

    def test_exec_result(self):
        r = self.desk().exec(OK, "hostname", shell="sh", timeout=10)
        self.assertEqual((r["exit"], r["remote_code"], r["stdout"], r["stderr"], r["route"], r["error"], r["desk"]),
                         (0, 0, "ran: hostname\n", "warn\n", "LAN", None, OK))

    def test_exec_stdin_argv_cwd(self):
        gd = self.desk()
        self.assertIn("stdin: a\nb\n", gd.exec(OK, ["wc", "-l"], stdin="a\nb\n")["stdout"])
        self.assertTrue(gd.exec(OK, ["wc", "-l"])["stdout"].startswith("ran: wc -l"))
        self.assertEqual(gd.exec(OK, "make", cwd="/srv/app")["stdout"], "ran: make\ncwd: /srv/app\n")

    def test_exec_nonzero_check_timeout(self):
        gd = self.desk()
        self.assertEqual(gd.exec(OK, "exit 3")["exit"], 3)
        with self.assertRaises(CommandError) as cm:
            gd.exec(OK, "exit 3", check=True)
        self.assertEqual(cm.exception.result["exit"], 3)
        t = gd.exec(OK, "sleep")
        self.assertEqual((t["timed_out"], t["exit"], t["error"]["kind"]), (True, 124, "failed"))

    def test_exec_failures_are_typed(self):
        gd = self.desk()
        with self.assertRaises(UnreachableError) as cm:
            gd.exec(OFFLINE, "x")
        e = cm.exception
        self.assertEqual((e.kind, e.reason, e.exit_code, e.desk), ("offline", "offline", 255, OFFLINE))
        with self.assertRaises(UsageError) as cm:
            gd.exec(USAGE, "x")
        self.assertIn("no credential", str(cm.exception))
        with self.assertRaises(RefusedError) as cm:
            gd.exec(REFUSED, "x")
        self.assertIn("`exec` scope", str(cm.exception))
        self.assertEqual((cm.exception.kind, cm.exception.exit_code), ("refused", 254))
        with self.assertRaises(GaiaDeskError) as cm:
            gd.exec(PLAIN, "x")
        self.assertEqual(str(cm.exception), "something odd")

    def test_bad_arguments_are_the_same_usage_error(self):
        gd = self.desk()
        for call in (lambda: gd.exec(OK, ""), lambda: gd.exec("-x", "x"), lambda: gd.exec(OK, "x", cwd=" "),
                     lambda: gd.run_job(OK, "-bad", "make"), lambda: gd.exec_stream(OK, "x", shell="fish")):
            with self.assertRaises(UsageError):
                call()

    # streams

    def test_exec_stream(self):
        s = self.desk().exec_stream(OK, "exit 2", cwd="/srv")
        self.assertEqual(text_of(s), ("part1 part2 exit 2\ncwd: /srv\n", "warn\n"))
        e = s.wait()
        self.assertEqual(e.exit_code, 2)
        self.assertEqual((s.result["event"], s.result["remote_code"], s.result["route"]), ("exit", 2, "LAN"))

    def test_exec_stream_stdin_up_front(self):
        s = self.desk().exec_stream(OK, "cat", stdin="hello")
        self.assertIn("stdin: hello", text_of(s)[0])
        self.assertEqual(s.wait().exit_code, 0)

    def test_exec_stream_never_ran(self):
        s = self.desk().exec_stream(OFFLINE, "x")
        self.assertEqual(list(s), [])
        e = s.wait()
        self.assertEqual(e.exit_code, 255)
        self.assertIn("is offline", e.stderr_tail)
        self.assertEqual((s.result["event"], s.result["error"]["kind"], s.result["error"]["reason"]), ("error", "unreachable", "offline"))

    def test_follow_job_logs(self):
        gd = self.desk()
        s = gd.follow_job_logs(OK, "build")
        self.assertEqual(text_of(s)[0], "one\ntwo\nthree\n")
        e = s.wait()
        self.assertEqual((e.exit_code, e.stderr_tail), (0, "job build exited (exit 0)"))
        self.assertEqual(s.result["event"], "end")
        lost = gd.follow_job_logs(OK, "lost")
        self.assertEqual(text_of(lost)[0], "one\ntwo\nthree\n")
        e = lost.wait()
        self.assertEqual((e.exit_code, e.stderr_tail), (255, "the connection to the desk was lost"))
        self.assertEqual(lost.result["error"]["kind"], "connection_lost")

    # jobs / stats / devices

    def test_jobs(self):
        gd = self.desk()
        j = gd.run_job(OK, "build", ["make", "-j8"], priority="low", cpu=50)
        self.assertEqual((j["name"], j["state"], j["command"]), ("build", "running", "make -j8"))
        with self.assertRaises(RefusedError) as cm:
            gd.run_job(REFUSED, "build", "make")
        self.assertEqual((cm.exception.exit_code, cm.exception.desk), (254, REFUSED))
        self.assertEqual([x["state"] for x in gd.jobs(OK)], ["running", "exited"])
        self.assertEqual(gd.job_logs(OK, "build"), "line1\nline2\n")
        self.assertEqual(gd.job_logs(OK, "build", tail=10), "tail\n")
        with self.assertRaises(OperationFailedError) as cm:
            gd.job_logs(OK, "nope")
        self.assertEqual((str(cm.exception), cm.exception.exit_code), ("no job named nope", 1))
        self.assertEqual(gd.kill_job(OK, "build")["state"], "killed")
        with self.assertRaises(OperationFailedError):
            gd.kill_job(OK, "nope")
        with self.assertRaises(ProtocolError):
            gd.jobs(PLAIN)

    def test_stats(self):
        gd = self.desk()
        s = gd.stats(OK)
        self.assertEqual((s["cpus"], s["desk"], s["hostname"]), (8, OK, "office-pc"))
        with self.assertRaises(UnreachableError) as cm:
            gd.stats(PLAIN)
        e = cm.exception
        self.assertEqual((str(e), e.kind, e.reason, e.desk), ("the desk did not answer", "timeout", "timeout", PLAIN))

    def test_devices(self):
        gd = self.desk()
        r = gd.devices()
        self.assertEqual([(d["desk_id"], d["online"], d["reachable"]) for d in r["devices"]], [(OK, True, None), (OFFLINE, False, None)])
        self.assertEqual((r["sources"], r["notes"]), (["account", "mesh"], []))
        self.assertEqual([d["name"] for d in gd.devices(desk_id=OK)["devices"]], ["office-pc"])

    # cp

    def test_cp_one_file(self):
        gd = self.desk()
        d = local_files()
        up = gd.upload(os.path.join(d, "app.txt"), OK, "deploy/")
        self.assertEqual((up["direction"], up["desk"], up["dirs"]), ("upload", OK, 0))
        if self.name == "api":
            self.assertEqual(up["destination"], "deploy/app.txt", "a remote folder keeps the file name")
        local = os.path.join(d, "got.log")
        down = gd.download(OK, "logs/app.log", local)
        self.assertEqual((down["direction"], down["desk"]), ("download", OK))
        if self.name == "api":
            with open(local) as f:
                self.assertEqual(f.read(), "contents of logs/app.log\n")
        with self.assertRaises(OperationFailedError) as cm:
            gd.upload(os.path.join(d, "fail.txt"), OK, "x/")
        self.assertEqual((len(cm.exception.json["failed"]), cm.exception.exit_code), (1, 1))
        with self.assertRaises(RefusedError) as cm:
            gd.upload(os.path.join(d, "app.txt"), REFUSED, "x/")
        self.assertEqual(cm.exception.desk, REFUSED)
        with self.assertRaises(GaiaDeskError) as cm:
            gd.upload(os.path.join(d, "app.txt"), PLAIN, "x/")
        self.assertIn("offline", str(cm.exception))
        self.assertEqual(cm.exception.exit_code, 255)
        with self.assertRaises(RefusedError):
            gd.download(REFUSED, "a.txt", local)

    # tokens

    def test_tokens(self):
        with self.assertRaises(RefusedError):
            self.anon().list_tokens(OK)
        gd = self.owner()
        made = gd.create_token([OK, OFFLINE], name="bot", scopes=["exec", "cp"])
        self.assertEqual([t["desk"] for t in made["tokens"]], [OK, OFFLINE])
        self.assertTrue(made["tokens"][0]["secret"].startswith("gdagt_"))
        self.assertEqual(made["tokens"][0]["token"]["label"], "bot")
        self.assertEqual(gd.list_tokens(OK)[0]["id"], "9f3a1c2b7d004e11")
        self.assertEqual(gd.revoke_token(OK, "bot"), {"revoked": "bot", "stopped_sessions": 1})
        with self.assertRaises(OperationFailedError) as cm:
            gd.revoke_token(OK, "ghost")
        self.assertIn("no live token", str(cm.exception))


class CliTransport(Shared, unittest.TestCase):
    name = "cli"

    def desk(self):
        return helpers.setup(GaiaDesk)[0]

    def owner(self):
        return helpers.setup(GaiaDesk, code="owner-pw")[0]

    def anon(self):
        return helpers.setup(GaiaDesk)[0]


class ApiTransport(Shared, unittest.TestCase):
    name = "api"

    def desk(self):
        return GaiaDesk(api_key="ak_test", desk_token="gdagt_test", base_url=API.url)

    def owner(self):
        return GaiaDesk(api_key="session-person", base_url=API.url)

    def anon(self):
        return GaiaDesk(api_key="ak_test", desk_token="gdagt_test", base_url=API.url)


if __name__ == "__main__":
    unittest.main()
