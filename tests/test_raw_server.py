"""A server or proxy that drops or stalls a connection, on a raw socket (no
HTTP framework): the API transport fails with a clear, typed error within its
timeouts and never hangs, and never sends a request twice. (The SDK has no
retry policy of its own for lost connections, and http.client never re-sends,
so every request reaches the server exactly once.)"""

import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

import helpers  # noqa: F401 (the package from src/)

sys.path.insert(0, os.path.join(helpers.HERE, "fixtures"))
import raw_server as rs  # noqa: E402

from gaiadesk import (  # noqa: E402
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_RESPONSE_TIMEOUT,
    AsyncGaiaDesk,
    ConnectionLostError,
    GaiaDesk,
    UnreachableError,
    UsageError,
)
from gaiadesk import _e2e  # noqa: E402
from gaiadesk._local import PipeSocket  # noqa: E402

D = "123456789"
BOUND = 10.0  # a hang shows as a failure after this long, not as a stuck run
BIG = b"\0" * (4 * 1024 * 1024)


def gd(server, idle=1.0, response=30.0, cls=GaiaDesk):
    return cls(api_key="ak_t", desk_token="gdagt_t", base_url=server.url, e2e="off", idle_timeout=idle, response_timeout=response)


def bounded(fn, *args):
    """``fn(*args)`` on a thread: ``(result, error, seconds)``, or a test failure if it is still running after BOUND."""
    out = {}

    def run():
        started = time.monotonic()
        try:
            out["r"] = fn(*args)
        except BaseException as e:  # noqa: BLE001 (handed to the test)
            out["e"] = e
        out["t"] = time.monotonic() - started

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(BOUND)
    if t.is_alive():
        raise AssertionError("no answer within %g s: the SDK hung" % BOUND)
    return out.get("r"), out.get("e"), out["t"]


class RawServerCase(unittest.TestCase):
    def server(self, mode):
        s = rs.RawServer(mode)
        self.addCleanup(s.close)
        return s

    def fails(self, cls, fn, *args):
        r, e, took = bounded(fn, *args)
        self.assertIsInstance(e, cls, "expected %s, got %r (result %r)" % (cls.__name__, e, r))
        return e, took


class DroppedBeforeAnyResponseByte(RawServerCase):
    def test_a_read_fails_unreachable_network_and_is_sent_once(self):
        for mode in (rs.CLOSE_BEFORE_RESPONSE, rs.RESET_BEFORE_RESPONSE):
            with self.subTest(mode=mode):
                s = self.server(mode)
                g = gd(s)
                e, _ = self.fails(UnreachableError, g.download_bytes, D, "/tmp/x")
                self.assertEqual((e.kind, e.reason, e.exit_code), ("network", "network", 255))
                self.assertEqual(s.count("GET"), 1)
                e, _ = self.fails(UnreachableError, g.stats, D)
                self.assertEqual(e.kind, "network")
                self.assertEqual(e.argv, ["GET /desks/123456789/stats"])
                self.assertEqual(s.count("GET"), 2)

    def test_a_large_upload_an_exec_or_a_job_is_never_sent_twice(self):
        for mode in (rs.CLOSE_BEFORE_RESPONSE, rs.RESET_BEFORE_RESPONSE, rs.CLOSE_AFTER_BODY):
            with self.subTest(mode=mode):
                s = self.server(mode)
                g = gd(s)
                e, _ = self.fails(UnreachableError, g.upload_bytes, BIG, D, "/tmp/big")
                self.assertEqual(e.kind, "network")
                self.assertEqual(s.count("PUT"), 1)
                with tempfile.NamedTemporaryFile(delete=False) as f:
                    f.write(BIG)
                self.addCleanup(os.unlink, f.name)
                self.fails(UnreachableError, g.upload, f.name, D, "/tmp/streamed")
                self.assertEqual(s.count("PUT"), 2)
                self.fails(UnreachableError, g.exec, D, "deploy")
                self.assertEqual(s.count("POST"), 1)
                st = g.exec_stream(D, "deploy")
                ex, _, _ = bounded(st.wait)
                self.assertEqual(ex.exit_code, 255)
                self.assertEqual(st.result["error"]["kind"], "unreachable")
                self.assertEqual(s.count("POST"), 2)
                self.fails(UnreachableError, g.run_job, D, "nightly", "make")
                self.assertEqual(s.count("POST"), 3)
                self.assertEqual(s.count("GET"), 0)


class Stalled(RawServerCase):
    def test_mid_download_connection_lost_timeout_within_the_idle_timeout_and_no_partial_file(self):
        s = self.server(rs.STALL_MID_BODY)
        g = gd(s, idle=1)
        e, took = self.fails(ConnectionLostError, g.download_bytes, D, "/tmp/x")
        self.assertEqual((e.kind, e.reason, e.exit_code), ("timeout", "timeout", 255))
        self.assertIn("idle_timeout", str(e))
        self.assertLess(took, 5)
        d = tempfile.mkdtemp(prefix="gaiadesk-stall-")
        self.addCleanup(lambda: os.path.isdir(d) and os.rmdir(d))
        dest = os.path.join(d, "x")
        e, took = self.fails(ConnectionLostError, g.download, D, "/tmp/x", dest)
        self.assertEqual(e.kind, "timeout")
        self.assertLess(took, 5)
        self.assertEqual(os.listdir(d), [], "a failed download leaves nothing behind")
        with open(dest, "wb") as f:
            f.write(b"before")
        self.fails(ConnectionLostError, g.download, D, "/tmp/x", dest)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"before", "an existing file is left as it was")
        self.assertEqual(os.listdir(d), ["x"])
        os.unlink(dest)

    @unittest.skipUnless(_e2e.AVAILABLE, "needs the cryptography package (gaiadesk[e2e])")
    def test_mid_sealed_download_connection_lost_timeout(self):
        # An end-to-end encrypted download: sealed NDJSON events, one line begun and never finished.
        s = self.server(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nTransfer-Encoding: chunked\r\n\r\n"
                        b"9\r\n{\"e2e\":\"x\r\n")
        g = GaiaDesk(api_key="ak_t", desk_token="gdagt_t", base_url=s.url, e2e="require", idle_timeout=1,
                     e2e_keys={D: "B6N8vBQgk8i3VdwbEOhstCY3StFqqFPtC9_AsrhtHHw"})
        e, took = self.fails(ConnectionLostError, g.download_bytes, D, "/tmp/x")
        self.assertEqual((e.kind, e.reason), ("timeout", "timeout"))
        self.assertIn("idle_timeout", str(e))
        self.assertLess(took, 6)  # the key lookup (GET /desks/{id}) stalls too, then the download
        self.assertEqual(s.count("GET"), 2)

    def test_mid_json_connection_lost_timeout(self):
        s = self.server(rs.STALL_MID_JSON)
        e, took = self.fails(ConnectionLostError, gd(s).stats, D)
        self.assertEqual((e.kind, e.reason), ("timeout", "timeout"))
        self.assertIn("idle_timeout", str(e))
        self.assertLess(took, 5)
        self.assertEqual(s.count("GET"), 1)

    def test_mid_stream_the_stream_ends_with_a_timeout_error(self):
        s = self.server(rs.STALL_MID_EVENTS)
        g = gd(s)
        st = g.exec_stream(D, "tail -f log")
        chunks, _, took = bounded(lambda: list(st.text()))
        self.assertEqual(chunks, [("stdout", "hi")])
        ex, _, _ = bounded(st.wait)
        self.assertEqual(ex.exit_code, 255)
        self.assertIn("idle_timeout", ex.stderr_tail)
        self.assertEqual(st.result["exit"], 255)
        self.assertEqual((st.result["error"]["kind"], st.result["error"]["reason"]), ("connection_lost", "timeout"))
        self.assertLess(took, 5)
        logs = g.follow_job_logs(D, "build")
        ex, _, _ = bounded(logs.wait)
        self.assertEqual(ex.exit_code, 255)
        self.assertEqual(logs.result["error"]["kind"], "connection_lost")

    def test_mid_stream_async(self):
        s = self.server(rs.STALL_MID_EVENTS)
        g = gd(s, cls=AsyncGaiaDesk)

        async def run():
            st = await g.exec_stream(D, "tail -f log")
            got = [c async for c in st.text()]
            return got, await st.wait(), st.result

        (got, ex, result), _, _ = bounded(asyncio.run, run())
        self.assertEqual(got, [("stdout", "hi")])
        self.assertEqual((ex.exit_code, result["error"]["kind"], result["error"]["reason"]), (255, "connection_lost", "timeout"))


class Silent(RawServerCase):
    def test_unreachable_timeout_within_the_response_timeout_and_never_sent_again(self):
        s = self.server(rs.SILENT)
        g = gd(s, response=1)
        e, took = self.fails(UnreachableError, g.stats, D)
        self.assertEqual((e.kind, e.reason, e.exit_code), ("timeout", "timeout", 255))
        self.assertIn("response_timeout", str(e))
        self.assertIn("within 1 s", str(e))
        self.assertLess(took, 5)
        e, took = self.fails(UnreachableError, g.upload_bytes, BIG, D, "/tmp/big")
        self.assertEqual(e.kind, "timeout")
        self.assertLess(took, 5)
        st = g.exec_stream(D, "deploy")
        ex, _, _ = bounded(st.wait)
        self.assertEqual((ex.exit_code, st.result["error"]["kind"], st.result["error"]["reason"]), (255, "unreachable", "timeout"))
        self.assertEqual((s.count("GET"), s.count("PUT"), s.count("POST")), (1, 1, 1))

    def test_the_idle_timeout_does_not_bound_the_wait_for_an_answer(self):
        # A held exec answers only when its command ends: the wait for headers is response_timeout's alone.
        s = self.server(rs.SILENT)
        g = gd(s, idle=0.3, response=1.5)
        e, took = self.fails(UnreachableError, g.exec, D, "sleep 600")
        self.assertEqual(e.kind, "timeout")
        self.assertGreater(took, 1.2)

    def test_killing_a_stream_still_stops_it_promptly(self):
        s = self.server(rs.SILENT)
        st = gd(s, response=600).exec_stream(D, "sleep 600")
        time.sleep(0.3)
        st.kill()
        ex, _, took = bounded(st.wait)
        self.assertEqual(ex.exit_code, 130)
        self.assertLess(took, 5)


class Stress(RawServerCase):
    def test_300_dropped_requests_never_hang(self):
        s = self.server(rs.CLOSE_BEFORE_RESPONSE)
        g = gd(s)
        up = b"\0" * (512 * 1024)
        modes = (rs.CLOSE_BEFORE_RESPONSE, rs.RESET_BEFORE_RESPONSE, rs.CLOSE_AFTER_BODY)
        for i in range(300):
            s.mode = modes[i % 3]
            if i % 2 == 0:
                _, e, _ = bounded(g.download_bytes, D, "/tmp/x")
            else:
                _, e, _ = bounded(g.upload_bytes, up, D, "/tmp/up")
            self.assertIsInstance(e, UnreachableError, "iteration %d (%s)" % (i, s.mode))
            self.assertEqual(e.kind, "network", "iteration %d (%s): %s" % (i, s.mode, e))
        self.assertEqual(s.count("PUT"), 150)  # every upload sent exactly once
        self.assertEqual(s.count("GET"), 150)  # and every read: no retry policy, and http.client never re-sends


class Options(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual((DEFAULT_RESPONSE_TIMEOUT, DEFAULT_IDLE_TIMEOUT), (960.0, 90.0))
        t = GaiaDesk(api_key="ak")._api
        self.assertEqual((t.response_timeout, t.idle_timeout), (960.0, 90.0))

    def test_timeouts_are_checked(self):
        for bad in (dict(idle_timeout=0), dict(response_timeout=-2), dict(idle_timeout="90"), dict(response_timeout=True),
                    dict(idle_timeout=float("nan")), dict(response_timeout=float("inf"))):
            with self.subTest(bad=bad), self.assertRaises(UsageError):
                GaiaDesk(api_key="ak", **bad)
        with self.assertRaises(UsageError):
            GaiaDesk(response_timeout=5)  # the CLI transport has its own
        none = GaiaDesk(api_key="ak", idle_timeout=None, response_timeout=None)._api
        self.assertEqual((none.response_timeout, none.idle_timeout), (None, None))
        t = GaiaDesk(api_key="ak", idle_timeout=2, response_timeout=0.5)._api
        self.assertEqual((t.response_timeout, t.idle_timeout), (0.5, 2.0))

    def test_local_and_lan_take_them_too(self):
        local = GaiaDesk(transport="local", socket_path="/nonexistent.sock", idle_timeout=3, response_timeout=4)._api
        self.assertEqual((local.response_timeout, local.idle_timeout), (4.0, 3.0))
        lan = GaiaDesk(transport="lan", base_url="https://10.0.0.2:7443/v1", fingerprint="ab" * 32, desk_token="gdagt_t",
                       idle_timeout=None)._api
        self.assertEqual((lan.response_timeout, lan.idle_timeout), (960.0, None))
        with self.assertRaises(UsageError):
            GaiaDesk(transport="local", socket_path="/x.sock", idle_timeout=0)


class LocalSocket(RawServerCase):
    """The local transport over a Unix socket: the same bounds."""

    def setUp(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("no Unix sockets here")

    def unix_server(self, answer):
        d = tempfile.mkdtemp(prefix="gd-", dir="/tmp" if os.path.isdir("/tmp") else None)  # a socket path has a ~104-byte limit
        path = os.path.join(d, "api.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(8)
        held = []

        def serve():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                held.append(c)
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if answer:
                    c.sendall(answer)

        threading.Thread(target=serve, daemon=True).start()

        def close():
            srv.close()
            for c in held:
                c.close()
            os.unlink(path)
            os.rmdir(d)

        self.addCleanup(close)
        return path

    def test_silent_and_stalled(self):
        path = self.unix_server(None)
        g = GaiaDesk(transport="local", socket_path=path, desk_token="gdagt_t", response_timeout=1)
        e, took = self.fails(UnreachableError, g.stats, D)
        self.assertEqual(e.kind, "timeout")
        self.assertIn("local API", str(e))
        self.assertLess(took, 5)
        path = self.unix_server(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"desk":')
        g = GaiaDesk(transport="local", socket_path=path, desk_token="gdagt_t", idle_timeout=1)
        e, took = self.fails(ConnectionLostError, g.stats, D)
        self.assertEqual((e.kind, e.reason), ("timeout", "timeout"))
        self.assertLess(took, 5)


class Pipe(unittest.TestCase):
    """A named pipe has no socket timeout: PipeSocket's watchdog cancels a read that blocks too long
    (on Windows with CancelIoEx; here a socket pair stands in for the pipe, closed to cancel)."""

    def test_a_blocked_read_times_out(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        f = a.makefile("rwb", buffering=0)

        class Stand(PipeSocket):
            def _cancel(self):
                a.shutdown(socket.SHUT_RDWR)

        p = Stand(f)
        self.addCleanup(p.close)
        r = p.makefile()
        b.sendall(b"hello")
        p.settimeout(0.5)
        self.assertEqual(r.read1(16), b"hello")
        _, e, took = bounded(r.read1, 16)
        self.assertIsInstance(e, socket.timeout)
        self.assertLess(took, 3)


if __name__ == "__main__":
    unittest.main()
