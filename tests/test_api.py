"""The API transport on its own: choosing it, what it sends (headers, bodies,
query), error envelopes, SSE parsing, the operations it does not serve, and
the asyncio client on it. (Behaviour shared with the CLI transport is in
test_transports.py.)"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import helpers
from helpers import OFFLINE, OK, PLAIN, USAGE

sys.path.insert(0, os.path.join(helpers.HERE, "fixtures"))
from mock_api import HTML_DESK, LIMITED_DESK, MockApi  # noqa: E402

from gaiadesk import (  # noqa: E402
    DEFAULT_API_URL,
    GaiaDeskError,
    AsyncGaiaDesk,
    GaiaDesk,
    OperationFailedError,
    ProtocolError,
    RefusedError,
    UnreachableError,
    UsageError,
)
from gaiadesk._api import SseEvent, SseParser, seconds  # noqa: E402

API = None


def setUpModule():
    global API
    API = MockApi()


def tearDownModule():
    API.close()


def api(cls=GaiaDesk, **kw):
    opts = dict(api_key="ak_test", desk_token="gdagt_test", base_url=API.url)
    opts.update(kw)
    return cls(**opts)


def body():
    return json.loads(API.last()["body"].decode("utf-8"))


NOT_SERVED = "not available over the API transport"


class Choosing(unittest.TestCase):
    def test_without_api_key_the_cli_transport_is_kept(self):
        before = len(API.requests)
        gd, calls = helpers.setup(GaiaDesk)
        self.assertEqual(gd.backend, "cli")
        self.assertEqual(gd.exec(OK, "hostname")["stdout"], "ran: hostname\n")
        self.assertEqual(gd.version(), "gaiadesk-cli 0.10.324")
        # gaiadesk_native forced absent (it may be installed here, and would then be the default).
        with mock.patch("gaiadesk._native.load_native", return_value=(None, "not installed")):
            self.assertEqual(GaiaDesk(env=helpers.base_env("/dev/null")).backend, "cli", "the default is unchanged")
        self.assertEqual(len(API.requests), before)

    def test_api_key_selects_the_api_transport(self):
        self.assertEqual(api().backend, "api")
        self.assertEqual(api(AsyncGaiaDesk).backend, "api")
        self.assertEqual(DEFAULT_API_URL, "https://api.gaiadesk.net/v1")
        for bad in (dict(desk_token="gdagt_x"), dict(base_url="http://x/v1"), dict(wake=5),
                    dict(api_key="ak_x", cli="/x/gaiadesk-cli"), dict(api_key="ak_x", backend="cli"),
                    dict(api_key=""), dict(api_key="ak_x", base_url="ftp://x"), dict(api_key="ak_x", wake=121)):
            with self.assertRaises(UsageError, msg=bad):
                GaiaDesk(**bad)


class Wire(unittest.TestCase):
    def test_headers_and_wake(self):
        api().stats(OK)
        h = API.last()["headers"]
        self.assertEqual((h["authorization"], h["x-gaiadesk-desk-token"]), ("Bearer ak_test", "gdagt_test"))
        self.assertEqual(API.last()["path"], "/v1/desks/%s/stats" % OK)
        api(wake=30).stats(OK)
        self.assertEqual(API.last()["query"], {"wake_s": "30"})
        GaiaDesk(api_key="session-person", base_url=API.url).list_tokens(OK)
        self.assertIsNone(API.last()["headers"]["x-gaiadesk-desk-token"], "no desk token unless one is given")

    def test_exec_sends_an_exec_spec(self):
        gd = api()
        gd.exec(OK, "hostname", shell="sh", timeout="10m", cwd="/srv", stdin=b"in")
        self.assertEqual((API.last()["method"], API.last()["headers"]["content-type"]), ("POST", "application/json"))
        self.assertEqual(body(), {"command": "hostname", "shell": "sh", "cwd": "/srv", "timeout_secs": 600, "stdin": "in"})
        gd.exec(OK, ["ls", "-l"])
        self.assertEqual(body(), {"argv": ["ls", "-l"]})
        gd.exec_stream(OK, "x").wait()
        self.assertEqual((API.last()["query"], API.last()["headers"]["accept"]), ({"stream": "1"}, "text/event-stream"))

    def test_env_and_shell_in_the_exec_and_job_specs(self):
        gd = api()
        gd.exec(OK, "deploy", shell="powershell", env={"STAGE": "prod", "EMPTY": ""})
        self.assertEqual(body(), {"command": "deploy", "shell": "pwsh", "env": {"STAGE": "prod", "EMPTY": ""}},
                         "powershell is sent as pwsh, as the CLI maps it")
        gd.exec_stream(OK, "x", env={"A": "1"}).wait()
        self.assertEqual(body(), {"command": "x", "env": {"A": "1"}})
        gd.run_job(OK, "build", "make all", shell="bash", env={"CI": "1"})
        self.assertEqual(body(), {"name": "build", "command": ["make all"], "limits": {}, "shell": "bash", "env": {"CI": "1"}})
        gd.run_job(OK, "build", "Get-Date", shell="powershell")
        self.assertEqual(body()["shell"], "pwsh")
        before = len(API.requests)
        with self.assertRaises(UsageError) as cm:
            gd.exec(OK, "x", env={"A=B": "secret-value"})
        self.assertNotIn("secret-value", str(cm.exception))
        for call in (lambda: gd.run_job(OK, "b", "x", env={"A": "nul\0"}), lambda: gd.run_job(OK, "b", "x", shell="none"),
                     lambda: gd.exec(OK, "x", shell="fish")):
            self.assertRaises(UsageError, call)
        self.assertEqual(len(API.requests), before, "nothing sent for a bad env or shell")

    def test_wait_job(self):
        from mock_api import WAITS

        gd = api()
        done = gd.wait_job(OK, "failing", timeout="10m")
        self.assertEqual((API.last()["method"], API.last()["path"], API.last()["query"]),
                         ("GET", "/v1/desks/%s/jobs/failing/wait" % OK, {"timeout": "600"}))
        self.assertEqual((done["timed_out"], done["job"]["state"], done["job"]["exit_code"]), (False, "exited", 3))
        now = gd.wait_job(OK, "slow", timeout=0)
        self.assertEqual((now["timed_out"], now["job"]["state"], API.last()["query"]["timeout"]), (True, "running", "0"))
        forever = gd.wait_job(OK, "build")
        self.assertEqual((forever["timed_out"], API.last()["query"]["timeout"]), (False, "870"))
        self.assertEqual(gd.wait_job(OK, "held")["job"]["name"], "held", "leading keep-alive spaces are still JSON")
        with self.assertRaises(GaiaDeskError) as cm:
            gd.wait_job(OK, "held-fail")
        self.assertEqual((cm.exception.kind, cm.exception.reason), ("connection_lost", "desk_disconnected"))
        # The held body is oneOf result | envelope: a late `failed` (error.status 422) is the CLI's error, not a result.
        with self.assertRaises(OperationFailedError) as cm:
            gd.wait_job(OK, "held-gone")
        self.assertEqual((cm.exception.kind, cm.exception.json["error"]["status"]), ("failed", 422))
        with self.assertRaises(OperationFailedError):
            gd.wait_job(OK, "nope")
        del WAITS[:]
        slow = gd.wait_job(OK, "slow", timeout=0.3)
        self.assertTrue(slow["timed_out"])
        self.assertTrue(WAITS and all(t == "1" for t in WAITS), WAITS)
        self.assertRaises(UsageError, lambda: gd.wait_job(OK, "-x"))

        async def go():
            r = await api(AsyncGaiaDesk).wait_job(OK, "failing")
            self.assertEqual(r["job"]["exit_code"], 3)

        asyncio.run(go())

    def test_job_and_token_specs(self):
        gd = api()
        gd.run_job(OK, "build", "make all", priority="low", cpu=50, mem="2G", keep_awake=True, cwd="src")
        self.assertEqual(body(), {"name": "build", "command": ["make all"], "cwd": "src",
                                  "limits": {"priority": "low", "cpu_percent": 50, "mem_mb": 2048, "keep_awake": True}})
        gd.job_logs(OK, "build", tail=10)
        self.assertEqual((API.last()["path"], API.last()["query"]), ("/v1/desks/%s/jobs/build/logs" % OK, {"tail": "10"}))
        owner = GaiaDesk(api_key="session-person", base_url=API.url)
        owner.create_token(OK, name="bot", expires="24h", cwd="/srv", low_priv=True)
        self.assertEqual(body(), {"name": "bot", "expires_secs": 86400, "scopes": ["exec", "cp", "jobs"], "cwd": "/srv", "low_priv": True})
        owner.revoke_token(OK, "9f3a1c2b7d004e11")
        self.assertEqual((API.last()["method"], API.last()["path"]), ("DELETE", "/v1/desks/%s/tokens/9f3a1c2b7d004e11" % OK))

    def test_bytes_up_and_down(self):
        gd = api()
        r = gd.upload_bytes("hello", OK, "notes/a.txt")
        last = API.last()
        self.assertEqual((last["method"], last["query"], last["headers"]["content-type"], last["body"]),
                         ("PUT", {"path": "notes/a.txt"}, "application/octet-stream", b"hello"))
        self.assertEqual(r["direction"], "upload")
        self.assertEqual(gd.download_bytes(OK, "notes/a.txt"), b"contents of notes/a.txt\n")
        d = tempfile.mkdtemp()
        down = gd.download(OK, "logs/app.log", d)
        self.assertEqual(down["destination"], os.path.join(d, "app.log"), "a local folder keeps the remote name")
        cli = helpers.setup(GaiaDesk)[0]
        with self.assertRaises(UsageError):
            cli.upload_bytes("x", OK, "a")
        with self.assertRaises(UsageError):
            cli.download_bytes(OK, "a")

    def test_seconds(self):
        self.assertEqual([seconds(v, "t") for v in (90, 1.2, "30s", "10m", "1h30m", "7d", "2w")], [90, 2, 30, 600, 5400, 604800, 1209600])
        with self.assertRaises(UsageError):
            seconds("5 fortnights", "t")


class Errors(unittest.TestCase):
    def test_envelopes(self):
        with self.assertRaises(RefusedError) as cm:
            GaiaDesk(api_key="ak_test", base_url=API.url).stats(OK)
        e = cm.exception
        self.assertEqual((e.kind, e.reason, e.status, e.exit_code, e.desk), ("refused", "desk_token_required", 403, 254, OK))
        self.assertRegex(e.request_id, r"^req_[0-9a-f]{24}$")
        with self.assertRaises(RefusedError) as cm:
            GaiaDesk(api_key="ak_test", base_url=API.url).list_tokens(OK)
        self.assertIn("signed-in person", str(cm.exception))
        with self.assertRaises(UnreachableError) as cm:
            api().exec(OFFLINE, "x")
        self.assertEqual((cm.exception.status, cm.exception.kind, cm.exception.reason, cm.exception.desk), (409, "offline", "offline", OFFLINE))
        with self.assertRaises(UsageError) as cm:
            api().exec(USAGE, "x")
        self.assertEqual((cm.exception.status, cm.exception.kind), (400, "usage"))
        with self.assertRaises(UnreachableError) as cm:
            api().stats(PLAIN)
        self.assertEqual((cm.exception.status, cm.exception.kind), (504, "timeout"))

    def test_rate_limit_html_and_network(self):
        before = len(API.requests)
        started = time.monotonic()
        with self.assertRaises(RefusedError) as cm:
            api(max_retry_wait=5, e2e="off").stats(LIMITED_DESK)  # a Retry-After past max_retry_wait is not waited for
        self.assertEqual((cm.exception.status, cm.exception.reason, cm.exception.retry_after), (429, "rate_limited", 7))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(API.requests) - before, 1)
        with self.assertRaises(ProtocolError) as cm:
            api().stats(HTML_DESK)
        self.assertEqual((cm.exception.kind, cm.exception.status), ("protocol", 500))
        self.assertIn("no error envelope", str(cm.exception))
        down = GaiaDesk(api_key="ak_test", base_url="http://127.0.0.1:1/v1", retry_base_delay=0.005)
        with self.assertRaises(UnreachableError) as cm:
            down.stats(OK)
        self.assertEqual((cm.exception.kind, cm.exception.reason, cm.exception.exit_code), ("network", "network", 255))
        s = down.exec_stream(OK, "x")
        e = s.wait()
        self.assertEqual((e.exit_code, s.result["error"]["kind"], s.result["error"]["reason"]), (255, "unreachable", "network"))

    def test_streams_refused_at_start_and_killed(self):
        s = GaiaDesk(api_key="ak_test", base_url=API.url).exec_stream(OK, "x")
        self.assertEqual(list(s), [])
        e = s.wait()
        self.assertEqual((e.exit_code, s.result["error"]["kind"], s.result["error"]["reason"]), (254, "refused", "desk_token_required"))
        f = api().follow_job_logs(OK, "build")
        f.kill()
        self.assertEqual(f.wait().exit_code, 130)
        with self.assertRaises(UsageError):
            api().exec_stream(OK, "x").write("more")

    def test_partial_mint_keeps_the_minted_tokens(self):
        owner = GaiaDesk(api_key="session-person", base_url=API.url)
        with self.assertRaises(ProtocolError) as cm:
            owner.create_token([OK, HTML_DESK], name="bot")
        self.assertEqual(cm.exception.json["tokens"][0]["desk"], OK)


class Sse(unittest.TestCase):
    def test_parser_any_chunking(self):
        text = (': keep-alive\r\nevent: stdout\r\ndata: {"event":"stdout","data":"a"}\r\n\r\n:ping\n\n'
                'event: x\ndata: line1\ndata: line2\n\ndata: {"event":"exit","exit":0}')
        want = [SseEvent("stdout", '{"event":"stdout","data":"a"}'), SseEvent("x", "line1\nline2"), SseEvent("message", '{"event":"exit","exit":0}')]
        for size in (1, 2, 3, 7, len(text)):
            p = SseParser()
            got = []
            for i in range(0, len(text), size):
                got += p.feed(text[i:i + size])
            got += p.end()
            self.assertEqual(got, want, "chunks of %d" % size)
        p = SseParser()
        self.assertEqual(p.feed("data: a\r"), [])
        self.assertEqual(p.feed("\ndata:b\r\r"), [], "a trailing \\r may be half of \\r\\n")
        self.assertEqual(p.feed("\n"), [SseEvent("message", "a\nb")])


class NotServed(unittest.TestCase):
    def test_usage_errors_and_nothing_sent(self):
        own = MockApi()  # its own server: nothing else in flight
        try:
            gd = GaiaDesk(api_key="ak_test", desk_token="gdagt_test", base_url=own.url)
            d = tempfile.mkdtemp()
            calls = [
                lambda: gd.shell(OK, "ls"),
                lambda: gd.shell_stream(OK),
                lambda: gd.measure(OK),
                lambda: gd.mesh_status(),
                lambda: gd.mesh_ip(OK),
                lambda: gd.disconnect(OK),
                lambda: gd.forward(OK, {"remote_port": 22}),
                lambda: gd.agent_connect(OK),
                lambda: gd.mcp(),
                lambda: gd.audit(OK),
                lambda: gd.version(),
                lambda: gd.cli_version_info(),
                lambda: gd.cli_features(),
                lambda: gd.raw(["devices"]),
                lambda: gd.probe(OK),
                lambda: gd.devices(probe=True),
                lambda: gd.upload(d, OK, "x/", recursive=True),
                lambda: gd.upload(d, OK, "x/"),
                lambda: gd.download(OK, "x/", d, recursive=True),
                lambda: gd.create_token(OK, name="bot", out="/tmp/bot.token"),
                lambda: gd.revoke_token(OK, all_for_desk=True),
                lambda: gd.revoke_token(OK, "bot", account=True),
                lambda: gd.exec_stream(OK, "cat", stdin=True),
                lambda: gd.exec_stream(OK, "cat", json_stream=False),
                lambda: gd.whoami(),
            ]
            for call in calls:
                with self.assertRaises(UsageError) as cm:
                    call()
                self.assertEqual(cm.exception.kind, "usage")
                self.assertIn(NOT_SERVED, str(cm.exception))
            self.assertEqual(own.requests, [])
        finally:
            own.close()


class Async(unittest.TestCase):
    def test_async_client_on_the_api(self):
        async def go():
            gd = api(AsyncGaiaDesk)
            r = await gd.exec(OK, "hostname", stdin="x")
            self.assertEqual((r["exit"], r["stdout"]), (0, "ran: hostname\nstdin: x\n"))
            with self.assertRaises(UnreachableError):
                await gd.exec(OFFLINE, "x")
            rs = await asyncio.gather(*(gd.exec(OK, "exit %d" % i) for i in range(4)))
            self.assertEqual([x["exit"] for x in rs], [0, 1, 2, 3])
            s = await gd.exec_stream(OK, "exit 2")
            out = "".join([t async for name, t in s.text() if name == "stdout"])
            self.assertEqual(out, "part1 part2 exit 2\n")
            self.assertEqual((await s.wait()).exit_code, 2)
            self.assertEqual(s.result["remote_code"], 2)
            f = await gd.follow_job_logs(OK, "build")
            self.assertEqual("".join([t async for _, t in f.text()]), "one\ntwo\nthree\n")
            self.assertEqual((await f.wait()).stderr_tail, "job build exited (exit 0)")
            self.assertEqual((await gd.run_job(OK, "build", "make"))["name"], "build")
            self.assertEqual(len(await gd.jobs(OK)), 2)
            self.assertEqual(await gd.job_logs(OK, "build"), "line1\nline2\n")
            with self.assertRaises(OperationFailedError):
                await gd.kill_job(OK, "nope")
            self.assertEqual((await gd.stats(OK))["cpus"], 8)
            self.assertEqual(len((await gd.devices())["devices"]), 2)
            self.assertEqual((await gd.upload_bytes(b"hi", OK, "a.txt"))["direction"], "upload")
            self.assertEqual(await gd.download_bytes(OK, "a.txt"), b"contents of a.txt\n")
            owner = GaiaDesk(api_key="session-person", base_url=API.url)
            self.assertEqual(owner.list_tokens(OK)[0]["label"], "bot")
            for call in (lambda: gd.measure(OK), lambda: gd.shell(OK, "ls"), lambda: gd.mesh_ip(OK), lambda: gd.forward(OK, {"remote_port": 1}),
                         lambda: gd.agent_connect(OK), lambda: gd.mcp(), lambda: gd.raw(["x"]), lambda: gd.shell_stream(OK)):
                with self.assertRaises(UsageError) as cm:
                    await call()
                self.assertIn(NOT_SERVED, str(cm.exception))

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
