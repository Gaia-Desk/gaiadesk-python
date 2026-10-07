"""The ``local`` and ``lan`` transports on their own: choosing them, their
credentials, the local API's socket and token file, the Windows pipe (over a
fake pipe), the LAN gateway's pinned certificate, and the pure helpers.
(The operations themselves run in test_transports.py, as on every transport.)"""

import asyncio
import io
import json
import os
import sys
import unittest
from unittest import mock

import helpers
from helpers import OK

sys.path.insert(0, os.path.join(helpers.HERE, "fixtures"))
from mock_desk_api import ADMIN_TOKEN, MockLanApi, MockLocalApi  # noqa: E402

from gaiadesk import (  # noqa: E402
    AsyncGaiaDesk,
    FingerprintMismatchError,
    GaiaDesk,
    OperationFailedError,
    RefusedError,
    UnreachableError,
    UsageError,
    certificate_fingerprint,
    default_local_address,
    local_api_dir,
    local_pipe_name,
    local_socket_path,
    local_token_path,
    normalize_fingerprint,
    pipe_user,
)
from gaiadesk._local import LOCAL_API_UNAVAILABLE  # noqa: E402

UNIX = hasattr(__import__("socket"), "AF_UNIX") and os.name != "nt"
LOCAL = None
LAN = None


def setUpModule():
    global LOCAL, LAN
    LAN = MockLanApi()
    if UNIX:
        LOCAL = MockLocalApi()


def tearDownModule():
    LAN.close()
    if LOCAL is not None:
        LOCAL.close()


class Choosing(unittest.TestCase):
    def test_defaults_are_unchanged(self):
        self.assertEqual(GaiaDesk(env=helpers.base_env("/dev/null"), backend="cli").backend, "cli")
        self.assertEqual(GaiaDesk(api_key="ak_x").backend, "api")
        self.assertEqual(GaiaDesk(transport="api", api_key="ak_x").backend, "api")
        self.assertEqual(GaiaDesk(transport="local").backend, "local")
        self.assertEqual(AsyncGaiaDesk(transport="local").backend, "local")
        lan = GaiaDesk(transport="lan", base_url=LAN.url, fingerprint=LAN.fingerprint, desk_token="gdagt_x")
        self.assertEqual(lan.backend, "lan")

    def test_bad_options_are_usage_errors(self):
        fp = LAN.fingerprint
        for bad in (dict(transport="ssh"), dict(transport="api"), dict(socket_path="/x"), dict(fingerprint=fp), dict(token="gdlocal_x"),
                    dict(transport="local", api_key="ak_x"), dict(transport="local", base_url="https://x/v1"),
                    dict(transport="local", fingerprint=fp), dict(transport="local", wake=5), dict(transport="local", backend="cli"),
                    dict(transport="local", desk_token="gdagt_x", token="gdlocal_x"), dict(transport="local", token=""),
                    dict(transport="local", socket_path=""), dict(api_key="ak_x", socket_path="/x"),
                    dict(transport="lan", fingerprint=fp, desk_token="gdagt_x"),
                    dict(transport="lan", base_url=LAN.url, desk_token="gdagt_x"),
                    dict(transport="lan", base_url=LAN.url, fingerprint=fp),
                    dict(transport="lan", base_url=LAN.url.replace("https:", "http:"), fingerprint=fp, desk_token="gdagt_x"),
                    dict(transport="lan", base_url=LAN.url, fingerprint="ab:cd", desk_token="gdagt_x"),
                    dict(transport="lan", base_url=LAN.url, fingerprint=fp, desk_token="gdagt_x", token="gdlocal_x"),
                    dict(transport="lan", base_url=LAN.url, fingerprint=fp, desk_token="gdagt_x", api_key="ak_x"),
                    dict(transport="lan", base_url=LAN.url, fingerprint=fp, desk_token="gdagt_x", cli="/x")):
            with self.assertRaises(UsageError, msg=bad):
                GaiaDesk(**bad)

    def test_lan_without_desk_token_says_why(self):
        with self.assertRaises(UsageError) as cm:
            GaiaDesk(transport="lan", base_url=LAN.url, fingerprint=LAN.fingerprint)
        self.assertIn("agent tokens only", str(cm.exception))


@unittest.skipUnless(UNIX, "the local transport is a Unix socket here (a named pipe on Windows)")
class Local(unittest.TestCase):
    def test_admin_token_from_the_token_file(self):
        with mock.patch.dict(os.environ, {"GAIADESK_API_DIR": LOCAL.dir}):
            r = GaiaDesk(transport="local").exec(OK, "hostname")
        self.assertEqual((r["exit"], r["stdout"], r["desk"]), (0, "ran: hostname\n", OK))
        rec = LOCAL.last()
        self.assertEqual((rec["method"], rec["path"], rec["host"]), ("POST", "/v1/desks/%s/exec" % OK, "localhost"))
        self.assertEqual(rec["headers"]["authorization"], "Bearer " + ADMIN_TOKEN, "the file's newline is stripped")
        self.assertIsNone(rec["headers"]["x-gaiadesk-desk-token"])

    def test_desk_token_instead_of_the_admin_token(self):
        gd = GaiaDesk(transport="local", env={"GAIADESK_API_DIR": LOCAL.dir}, desk_token="gdagt_test")
        self.assertEqual(gd.stats(OK)["desk"], OK)
        h = LOCAL.last()["headers"]
        self.assertEqual((h["x-gaiadesk-desk-token"], h["authorization"]), ("gdagt_test", None))

    def test_explicit_token_and_socket_path(self):
        gd = GaiaDesk(transport="local", socket_path=LOCAL.socket_path, token=ADMIN_TOKEN, env={"GAIADESK_API_DIR": "/nowhere"})
        self.assertEqual(gd.jobs(OK)[0]["state"], "running")
        self.assertEqual(LOCAL.last()["headers"]["authorization"], "Bearer " + ADMIN_TOKEN)

    def test_a_401_envelope_is_the_typed_error(self):
        gd = GaiaDesk(transport="local", socket_path=LOCAL.socket_path, token="gdlocal_wrong")
        with self.assertRaises(RefusedError) as cm:
            gd.stats(OK)
        e = cm.exception
        self.assertEqual((e.status, e.kind, e.reason, e.exit_code), (401, "refused", "unauthenticated", 254))
        self.assertRegex(e.request_id, r"^req_[0-9a-f]{24}$")

    def test_streamed_exec_files_and_held_wait(self):
        gd = GaiaDesk(transport="local", env={"GAIADESK_API_DIR": LOCAL.dir})
        s = gd.exec_stream(OK, "exit 2")
        out = "".join(t for name, t in s.text() if name == "stdout")
        self.assertEqual((out, s.wait().exit_code, s.result["event"]), ("part1 part2 exit 2\n", 2, "exit"))
        self.assertEqual(gd.download_bytes(OK, "logs/a.log"), b"contents of logs/a.log\n")
        self.assertEqual(gd.upload_bytes("hi", OK, "deploy/x.txt")["direction"], "upload")
        self.assertEqual(gd.wait_job(OK, "held")["job"]["name"], "held")
        with self.assertRaises(OperationFailedError):
            gd.wait_job(OK, "held-gone")

    def test_async(self):
        async def go():
            gd = AsyncGaiaDesk(transport="local", env={"GAIADESK_API_DIR": LOCAL.dir})
            r = await gd.exec(OK, "hostname")
            s = await gd.exec_stream(OK, "exit 2")
            chunks = [c async for c in s]
            return r, chunks, await s.wait()

        r, chunks, e = asyncio.run(go())
        self.assertEqual((r["stdout"], e.exit_code), ("ran: hostname\n", 2))
        self.assertTrue(chunks)

    def test_missing_socket_is_the_clear_unreachable_error(self):
        gd = GaiaDesk(transport="local", socket_path=os.path.join(LOCAL.dir, "gone.sock"), token=ADMIN_TOKEN)
        with self.assertRaises(UnreachableError) as cm:
            gd.exec(OK, "x")
        e = cm.exception
        self.assertEqual((e.kind, e.reason, e.exit_code), ("unreachable", "local_api_unavailable", 255))
        self.assertIn(LOCAL_API_UNAVAILABLE, str(e))
        self.assertIn("Settings → GaiaDesk API → Local API", str(e))
        s = gd.exec_stream(OK, "x")
        self.assertEqual((s.wait().exit_code, s.result["error"]["reason"]), (255, "local_api_unavailable"))

    def test_missing_token_file_is_unreachable_too(self):
        empty = os.path.join(LOCAL.dir, "empty")
        os.makedirs(empty, exist_ok=True)
        gd = GaiaDesk(transport="local", socket_path=LOCAL.socket_path, env={"GAIADESK_API_DIR": empty})
        with self.assertRaises(UnreachableError) as cm:
            gd.stats(OK)
        self.assertEqual(cm.exception.reason, "local_api_unavailable")
        self.assertIn("give desk_token", str(cm.exception))


class Lan(unittest.TestCase):
    def lan(self, fingerprint=None, cls=GaiaDesk):
        return cls(transport="lan", base_url=LAN.url, fingerprint=fingerprint or LAN.fingerprint, desk_token="gdagt_test")

    def test_the_right_fingerprint(self):
        r = self.lan().exec(OK, "hostname")
        self.assertEqual((r["exit"], r["stdout"]), (0, "ran: hostname\n"))
        h = LAN.last()["headers"]
        self.assertEqual((h["x-gaiadesk-desk-token"], h["authorization"]), ("gdagt_test", None))
        self.assertEqual(LAN.last()["path"], "/v1/desks/%s/exec" % OK)

    def test_fingerprint_forms(self):
        fp = LAN.fingerprint
        colons = ":".join(fp[i:i + 2] for i in range(0, 64, 2))
        for form in (fp, fp.upper(), colons, colons.upper(), " " + colons + "\n"):
            self.assertEqual(normalize_fingerprint(form), colons)
            self.assertEqual(self.lan(form).stats(OK)["desk"], OK)

    def test_a_wrong_fingerprint_sends_nothing(self):
        before = len(LAN.requests)
        wrong = "00" * 32
        with self.assertRaises(FingerprintMismatchError) as cm:
            self.lan(wrong).exec(OK, "hostname")
        e = cm.exception
        self.assertIsInstance(e, UnreachableError)
        self.assertEqual((e.kind, e.reason, e.exit_code), ("unreachable", "fingerprint_mismatch", 255))
        self.assertIn("pinned", str(e))
        self.assertIn(normalize_fingerprint(LAN.fingerprint), str(e))
        s = self.lan(wrong).exec_stream(OK, "x")
        self.assertEqual((s.wait().exit_code, s.result["error"]["reason"]), (255, "fingerprint_mismatch"))
        self.assertEqual(len(LAN.requests), before, "the server never got a request")

    def test_the_gateway_refuses_the_admin_token(self):
        # What the gateway answers a Bearer (the SDK never sends one on the LAN).
        gd = self.lan()
        gd._api.headers = lambda: {"Authorization": "Bearer " + ADMIN_TOKEN}
        with self.assertRaises(RefusedError) as cm:
            gd.stats(OK)
        self.assertEqual((cm.exception.status, cm.exception.reason), (401, "admin_token_local_only"))

    def test_async(self):
        async def go():
            return await self.lan(cls=AsyncGaiaDesk).exec(OK, "hostname")

        self.assertEqual(asyncio.run(go())["stdout"], "ran: hostname\n")


class FakePipe(io.RawIOBase):
    """A named pipe's file: records what is written, answers a canned response."""

    def __init__(self, answer):
        super().__init__()
        self.sent = b""
        self._answer = io.BytesIO(answer)

    def writable(self):
        return True

    def readable(self):
        return True

    def write(self, b):
        self.sent += bytes(b)
        return len(b)

    def readinto(self, b):
        return self._answer.readinto(b)


class Pipe(unittest.TestCase):
    def test_http_over_a_named_pipe(self):
        result = {"exit": 0, "remote_code": 0, "stdout": "ran\n", "stderr": "", "desk": OK, "route": "LAN", "error": None, "timed_out": False}
        body = json.dumps(result).encode("utf-8")
        answer = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body
        pipe = FakePipe(answer)
        opened = []

        def fake_open(name, mode, buffering=-1):
            opened.append((name, mode, buffering))
            return pipe

        with mock.patch("gaiadesk._local.open", fake_open, create=True):
            gd = GaiaDesk(transport="local", socket_path="\\\\.\\pipe\\gaiadesk-api-alice", token=ADMIN_TOKEN)
            r = gd.exec(OK, "hostname")
        self.assertEqual(r["stdout"], "ran\n")
        self.assertEqual(opened, [("\\\\.\\pipe\\gaiadesk-api-alice", "r+b", 0)])
        head = pipe.sent.split(b"\r\n\r\n", 1)[0].decode()
        self.assertTrue(head.startswith("POST /v1/desks/%s/exec HTTP/1.1" % OK))
        self.assertIn("Host: localhost", head)
        self.assertIn("Authorization: Bearer " + ADMIN_TOKEN, head)

    def test_missing_pipe(self):
        def no_pipe(name, mode, buffering=-1):
            raise FileNotFoundError(2, "The system cannot find the file specified", name)

        with mock.patch("gaiadesk._local.open", no_pipe, create=True):
            gd = GaiaDesk(transport="local", socket_path="//./pipe/gaiadesk-api-alice", token=ADMIN_TOKEN)
            with self.assertRaises(UnreachableError) as cm:
                gd.stats(OK)
        self.assertEqual(cm.exception.reason, "local_api_unavailable")


class Helpers(unittest.TestCase):
    def test_pipe_user(self):
        self.assertEqual(pipe_user("Alice"), "alice")
        self.assertEqual(pipe_user("DOMAIN\\Bob Smith"), "domain_bob_smith")
        self.assertEqual(pipe_user("a.b_c-d9"), "a.b_c-d9")
        self.assertEqual(pipe_user("Jöhn"), "j_hn")
        self.assertEqual(pipe_user(""), "user")
        self.assertEqual(pipe_user("x" * 100), "x" * 64)

    def test_pipe_name(self):
        self.assertEqual(local_pipe_name({"USERNAME": "Alice"}), "\\\\.\\pipe\\gaiadesk-api-alice")
        self.assertEqual(local_pipe_name({"USERNAME": "Alice", "GAIADESK_API_PIPE": "\\\\.\\pipe\\mine"}), "\\\\.\\pipe\\mine")
        with mock.patch("getpass.getuser", return_value="Carol"):
            self.assertEqual(local_pipe_name({}), "\\\\.\\pipe\\gaiadesk-api-carol")
        with mock.patch("getpass.getuser", side_effect=OSError("no user")):
            self.assertEqual(local_pipe_name({}), "\\\\.\\pipe\\gaiadesk-api-user")

    def test_default_paths(self):
        d = os.path.abspath(os.path.join(os.sep, "srv", "gd"))
        env = {"GAIADESK_API_DIR": d}
        self.assertEqual(local_api_dir(env), d)
        self.assertEqual(local_socket_path(env), os.path.join(d, "api.sock"))
        self.assertEqual(local_token_path(env), os.path.join(d, "api-token"))
        home = os.path.join(os.path.expanduser("~"), ".gaiadesk")
        self.assertEqual(local_api_dir({}), home)
        self.assertEqual(local_api_dir({"GAIADESK_API_DIR": "relative/dir"}), home, "only an absolute dir counts")
        self.assertEqual(local_socket_path({}), os.path.join(home, "api.sock"))
        self.assertEqual(local_token_path({}), os.path.join(home, "api-token"))
        want = local_pipe_name(env) if os.name == "nt" else local_socket_path(env)
        self.assertEqual(default_local_address(env), want)

    def test_fingerprints(self):
        self.assertEqual(normalize_fingerprint("AB" * 32), ":".join(["ab"] * 32))
        self.assertEqual(certificate_fingerprint(b"x"), normalize_fingerprint("2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"))
        for bad in ("", "ab:cd", "zz" * 32, "ab" * 33, None, 5):
            with self.assertRaises(UsageError, msg=bad):
                normalize_fingerprint(bad)


if __name__ == "__main__":
    unittest.main()
