"""The retry policy, on a raw socket (tests/fixtures/raw_server.py): a request is
sent again only when that cannot run anything twice. Never established: any
method. Lost after sending, 502/503/504: GETs only (not a 503 saying the API is
off). 429 and 409 idempotency_key_in_flight: any method. Never a timeout;
an Idempotency-Key does not make a call retryable."""

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
from test_raw_server import BOUND, D, bounded  # noqa: E402

from gaiadesk import (  # noqa: E402
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT,
    DEFAULT_RETRY_BASE_DELAY,
    DEFAULT_RETRY_MAX_DELAY,
    ConnectionLostError,
    GaiaDesk,
    GaiaDeskError,
    RefusedError,
    UnreachableError,
    UsageError,
)
from gaiadesk import _e2e  # noqa: E402
from gaiadesk._retry import RetryPolicy  # noqa: E402

KEY = "B6N8vBQgk8i3VdwbEOhstCY3StFqqFPtC9_AsrhtHHw"


def gd(url, retries=2, base=0.005, **kw):
    opts = dict(api_key="ak_t", desk_token="gdagt_t", base_url=url, e2e="off", response_timeout=30, idle_timeout=1,
                max_retries=retries, retry_base_delay=base)
    opts.update(kw)
    return GaiaDesk(**opts)


class Case(unittest.TestCase):
    def server(self, mode, port=0):
        s = rs.RawServer(mode, port)
        self.addCleanup(s.close)
        return s

    def fails(self, cls, fn, *args, **kw):
        r, e, took = bounded(lambda: fn(*args, **kw))
        self.assertIsInstance(e, cls, "expected %s, got %r (result %r)" % (cls.__name__, e, r))
        return e, took


class NeverEstablished(Case):
    def test_refused_then_the_server_appears_a_post_succeeds_sent_once(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # nothing listens there now: connections are refused
        servers = []
        later = threading.Timer(0.1, lambda: servers.append(rs.RawServer(rs.OK, port)))
        later.start()
        self.addCleanup(lambda: [s.close() for s in servers])
        url = "http://127.0.0.1:%d/v1" % port
        r, e, _ = bounded(gd(url, retries=4, base=0.06).exec, D, "deploy")  # waits >= 30+60+120 ms in all
        later.join()
        self.assertIsNone(e)
        self.assertEqual(r["exit"], 0)
        self.assertEqual(servers[0].count("POST"), 1)

    def test_refused_with_retries_off_fails_at_once(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        e, took = self.fails(UnreachableError, gd("http://127.0.0.1:%d/v1" % port, retries=0).exec, D, "deploy")
        self.assertEqual(e.kind, "network")
        self.assertLess(took, 1)

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "no Unix sockets here")
    def test_a_local_socket_that_is_not_there_yet(self):
        d = tempfile.mkdtemp(prefix="gd-", dir="/tmp" if os.path.isdir("/tmp") else None)
        path = os.path.join(d, "api.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(lambda: (srv.close(), os.path.exists(path) and os.unlink(path), os.rmdir(d)))
        posts = []

        def appear():
            time.sleep(0.15)
            srv.bind(path)
            srv.listen(4)
            c, _ = srv.accept()
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += c.recv(65536)
            posts.append(buf.split(b" ", 1)[0])
            body = b'{"exit":0,"stdout":"","stderr":""}'
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            c.close()

        threading.Thread(target=appear, daemon=True).start()
        g = GaiaDesk(transport="local", socket_path=path, desk_token="gdagt_t", max_retries=4, retry_base_delay=0.08)
        r, e, _ = bounded(g.exec, D, "deploy")
        self.assertIsNone(e)
        self.assertEqual((r["exit"], posts), (0, [b"POST"]))


class LostAfterSending(Case):
    def test_only_gets_are_sent_again(self):
        for mode in (rs.CLOSE_BEFORE_RESPONSE, rs.RESET_BEFORE_RESPONSE, rs.CLOSE_AFTER_BODY):
            with self.subTest(mode=mode):
                s = self.server(mode)
                g = gd(s.url)
                self.fails(UnreachableError, g.stats, D)
                self.assertEqual(s.count("GET"), 3)
                self.fails(UnreachableError, g.exec, D, "deploy")
                self.fails(UnreachableError, g.upload_bytes, b"x" * 65536, D, "/tmp/f")
                self.fails(UnreachableError, g.kill_job, D, "nightly")
                self.fails(UnreachableError, g.revoke_token, D, "tok_1")
                # An Idempotency-Key is sent, but does not make a POST retryable.
                self.fails(UnreachableError, g._api.call, "POST", "/desks/%s/exec" % D, json={"command": "x"},
                           headers={"Idempotency-Key": "k1"})
                self.assertEqual((s.count("POST"), s.count("PUT"), s.count("DELETE")), (2, 1, 2))
                self.assertIn("Idempotency-Key: k1", s.heads[-1])

    def test_keep_alive_then_close_nothing_is_sent_twice(self):
        # Each request has a connection of its own: the server's close on a second request never happens,
        # and every call reaches it exactly once.
        s = self.server(rs.KEEP_ALIVE_THEN_CLOSE)
        g = gd(s.url)
        for fn, args in ((g.jobs, (D,)), (g.kill_job, (D, "nightly")), (g.run_job, (D, "nightly", "make")),
                         (g.upload_bytes, (b"x", D, "/tmp/f"))):
            r, e, _ = bounded(fn, *args)
            self.assertIsNone(e, fn.__name__)
        self.assertEqual((s.count("GET"), s.count("DELETE"), s.count("POST"), s.count("PUT")), (1, 1, 1, 1))


class Statuses(Case):
    def test_502_503_504_gets_only(self):
        for code in (502, 503, 504):
            with self.subTest(code=code):
                s = self.server(rs.status(code))
                g = gd(s.url)
                e, _ = self.fails(GaiaDeskError, g.stats, D)
                self.assertEqual(e.status, code)
                self.assertEqual(s.count("GET"), 3)
                e, _ = self.fails(GaiaDeskError, g.exec, D, "deploy")
                self.assertEqual((e.status, s.count("POST")), (code, 1))

    def test_a_503_saying_it_is_switched_off_is_not_retried(self):
        for reason in ("api_disabled", "desk_ops_disabled", "local_api_off"):
            with self.subTest(reason=reason):
                s = self.server(rs.status(503, reason=reason))
                e, _ = self.fails(GaiaDeskError, gd(s.url).stats, D)
                self.assertEqual((e.status, e.reason, s.count("GET")), (503, reason, 1))

    def test_503_retry_after_is_waited_for(self):
        s = self.server(rs.status(503, retry_after=1))
        e, took = self.fails(GaiaDeskError, gd(s.url, retries=1).stats, D)
        self.assertEqual((e.status, e.retry_after, s.count("GET")), (503, 1, 2))
        self.assertGreaterEqual(took, 0.9)

    def test_429_any_method_after_retry_after(self):
        s = self.server(rs.status(429, retry_after=0, reason="rate_limited"))
        e, _ = self.fails(RefusedError, gd(s.url).exec, D, "deploy")
        self.assertEqual((e.status, e.reason, s.count("POST")), (429, "rate_limited", 3))
        s = self.server(rs.status(429, retry_after=0, reason="desk_busy", keep_alive=True))
        self.fails(RefusedError, gd(s.url).upload_bytes, b"x" * 100000, D, "/tmp/f")
        self.assertEqual(s.count("PUT"), 3)

    def test_429_retry_after_past_max_retry_wait_is_raised_at_once(self):
        s = self.server(rs.status(429, retry_after=120, reason="rate_limited"))
        e, took = self.fails(RefusedError, gd(s.url).stats, D)
        self.assertEqual((e.retry_after, s.count("GET")), (120, 1))
        self.assertLess(took, 1)

    def test_409_idempotency_key_in_flight_any_method(self):
        s = self.server(rs.status(409, reason="idempotency_key_in_flight"))
        g = gd(s.url)
        self.fails(GaiaDeskError, g.exec, D, "deploy")
        self.fails(GaiaDeskError, g.upload_bytes, b"x", D, "/tmp/f")
        self.fails(GaiaDeskError, g.kill_job, D, "nightly")
        self.assertEqual((s.count("POST"), s.count("PUT"), s.count("DELETE")), (3, 3, 3))

    def test_other_statuses_are_not_retried(self):
        for code, reason in ((500, None), (409, "offline"), (403, "forbidden")):
            with self.subTest(code=code):
                s = self.server(rs.status(code, reason=reason))
                self.fails(GaiaDeskError, gd(s.url).stats, D)
                self.assertEqual(s.count("GET"), 1)


class Never(Case):
    def test_timeouts_are_not_retried(self):
        s = self.server(rs.SILENT)
        e, _ = self.fails(UnreachableError, gd(s.url, response_timeout=0.5).stats, D)
        self.assertEqual((e.kind, s.count("GET")), ("timeout", 1))
        s = self.server(rs.STALL_MID_JSON)
        e, _ = self.fails(ConnectionLostError, gd(s.url, idle_timeout=0.5).stats, D)
        self.assertEqual((e.kind, s.count("GET")), ("timeout", 1))

    def test_retries_off_every_mode_once(self):
        modes = (rs.CLOSE_BEFORE_RESPONSE, rs.RESET_BEFORE_RESPONSE, rs.CLOSE_AFTER_BODY, rs.status(502), rs.status(503),
                 rs.status(504), rs.status(429, retry_after=0), rs.status(409, reason="idempotency_key_in_flight"))
        for mode in modes:
            with self.subTest(mode=mode):
                s = self.server(mode)
                g = gd(s.url, retries=0)
                self.fails(GaiaDeskError, g.stats, D)
                self.fails(GaiaDeskError, g.exec, D, "deploy")
                self.assertEqual((s.count("GET"), s.count("POST")), (1, 1))

    def test_kill_during_a_backoff_wait_stops_at_once(self):
        s = self.server(rs.status(429, retry_after=30, reason="rate_limited"))
        st = gd(s.url).follow_job_logs(D, "build")
        deadline = time.monotonic() + BOUND
        while s.count("GET") < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        started = time.monotonic()
        st.kill()
        ex, _, _ = bounded(st.wait)
        self.assertEqual(ex.exit_code, 130)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(s.count("GET"), 1)

    @unittest.skipUnless(_e2e.AVAILABLE, "needs the cryptography package (gaiadesk[e2e])")
    def test_a_sealed_request_is_sealed_afresh_for_each_attempt(self):
        s = self.server(rs.status(503))
        g = gd(s.url, e2e="require", e2e_keys={D: KEY})
        self.fails(GaiaDeskError, g.stats, D)
        sealed = [h for h in s.heads if h.startswith("GET /v1/desks/%s/stats" % D)]
        self.assertEqual(len(sealed), 3)
        seals = [line for h in sealed for line in h.split("\r\n") if line.lower().startswith("gaiadesk-e2e:")]
        self.assertEqual(len(seals), 3)
        self.assertEqual(len(set(seals)), 3, "each attempt has its own seal")


class Policy(unittest.TestCase):
    def test_defaults_and_backoff(self):
        self.assertEqual((DEFAULT_MAX_RETRIES, DEFAULT_RETRY_BASE_DELAY, DEFAULT_RETRY_MAX_DELAY, DEFAULT_MAX_RETRY_WAIT),
                         (2, 0.25, 8.0, 60.0))
        p = RetryPolicy()
        for n, full in ((0, 0.25), (1, 0.5), (2, 1.0), (4, 4.0), (5, 8.0), (9, 8.0)):
            for _ in range(50):
                d = p.delay(n)
                self.assertTrue(full * 0.5 <= d <= full, (n, d))
        lo, hi = RetryPolicy(rand=lambda a, b: a), RetryPolicy(rand=lambda a, b: b)
        self.assertEqual((lo.delay(0), hi.delay(0), hi.delay(10)), (0.125, 0.25, 8.0))
        self.assertEqual((p.delay(3, 7), p.delay(0, 60), p.delay(0, 0)), (7, 60, 0))
        self.assertIsNone(p.delay(0, 61))
        t = GaiaDesk(api_key="ak")._api.retry
        self.assertEqual((t.max_retries, t.base_delay, t.max_delay, t.max_wait), (2, 0.25, 8.0, 60.0))

    def test_options_are_checked(self):
        for bad in (dict(max_retries=-1), dict(max_retries=1.5), dict(max_retries=True), dict(retry_base_delay=-0.1),
                    dict(retry_max_delay=float("nan")), dict(max_retry_wait=-1), dict(retry_base_delay="1")):
            with self.subTest(bad=bad), self.assertRaises(UsageError):
                GaiaDesk(api_key="ak", **bad)
        with self.assertRaises(UsageError):
            GaiaDesk(max_retries=0)  # the CLI transport has its own
        t = GaiaDesk(transport="local", socket_path="/x.sock", max_retries=0, retry_max_delay=1)._api.retry
        self.assertEqual((t.max_retries, t.max_delay), (0, 1.0))
        t = GaiaDesk(transport="lan", base_url="https://10.0.0.2:7443/v1", fingerprint="ab" * 32, desk_token="gdagt_t",
                     max_retries=5)._api.retry
        self.assertEqual(t.max_retries, 5)


if __name__ == "__main__":
    unittest.main()
