"""The API transport, end-to-end encrypted, against a mock API that is also the desk
(mock_e2e_api): every operation sealed returns and raises exactly what it does in
the clear (the same operation against mock_api), and nothing the caller sealed (a
canary in the command, an env value, stdin, a path, a file's bytes) ever reaches
"the server". Then the policy: e2e=auto/require/off, no key, no cryptography,
pinned keys, and the two retries (e2e_required, e2e_decrypt_failed)."""

import asyncio
import os
import sys
import tempfile
import unittest
import warnings

import helpers
from helpers import OK, REFUSED

sys.path.insert(0, os.path.join(helpers.HERE, "fixtures"))
from mock_api import MockApi  # noqa: E402
from mock_e2e_api import PLACEHOLDER, MockE2eApi  # noqa: E402

from gaiadesk import (  # noqa: E402
    AsyncGaiaDesk,
    ConnectionLostError,
    EndToEndError,
    GaiaDesk,
    GaiaDeskError,
    OperationFailedError,
    RefusedError,
    UsageError,
)
from gaiadesk import _api_e2e, _e2e  # noqa: E402

CANARY = "canary-7f3e9b"
VECTOR_PUB = "B6N8vBQgk8i3VdwbEOhstCY3StFqqFPtC9_AsrhtHHw"  # vectors.json's desk_pub: a real key, no crypto needed to list it
needs_crypto = unittest.skipUnless(_e2e.AVAILABLE, "needs the cryptography package (gaiadesk[e2e])")


def client(api, cls=GaiaDesk, key="ak_test", **kw):
    opts = dict(api_key=key, base_url=api.url)
    if key.startswith("ak_"):
        opts["desk_token"] = "gdagt_test"
    opts.update(kw)
    return cls(**opts)


def op_requests(api):
    """The desk operations sent (not the desk lookups and wakes)."""
    return [r for r in api.requests if r["path"].count("/") > 3 and not r["path"].endswith("/wake")]


class Base(unittest.TestCase):
    def setUp(self):
        _api_e2e._warned.clear()
        self.api = MockE2eApi()
        self.plain = MockApi()
        if _e2e.AVAILABLE:
            for d in (OK, REFUSED):
                self.api.desks[d] = {"secret": os.urandom(32), "listed": True}

    def tearDown(self):
        self.api.close()
        self.plain.close()

    def assertNothingInTheClear(self):
        self.assertTrue(self.api.raw)
        for raw in self.api.raw:
            self.assertNotIn(CANARY.encode(), raw)

    def both(self, call, key="ak_test", **kw):
        """``call(client)`` on the plaintext mock and sealed: both answers."""
        return call(client(self.plain, key=key)), call(client(self.api, key=key, e2e="require", **kw))


@needs_crypto
class SealedOperations(Base):
    def test_exec_is_sealed_and_answers_the_same(self):
        run = lambda gd: gd.exec(OK, "echo " + CANARY, stdin=CANARY, env={"SECRET": CANARY}, cwd="/srv/" + CANARY, shell="bash")
        plain, sealed = self.both(run)
        self.assertEqual(sealed, plain)
        self.assertIn(CANARY, sealed["stdout"])
        last = op_requests(self.api)[-1]
        self.assertEqual(list(__import__("json").loads(last["body"])), ["e2e"], "the body is the sealed request alone")
        spec = self.api.opened[-1]["spec"]
        self.assertEqual((spec["env"], spec["stdin"], spec["cwd"]), ({"SECRET": CANARY}, CANARY, "/srv/" + CANARY))
        self.assertNothingInTheClear()

    def test_exec_stream_maps_the_sealed_events_like_the_plaintext_stream(self):
        def run(gd):
            s = gd.exec_stream(OK, "echo é " + CANARY, env={"K": CANARY})
            text = list(s.text())
            return "".join(t for n, t in text if n == "stdout"), "".join(t for n, t in text if n == "stderr"), s.wait(), s.result

        plain, sealed = self.both(run)
        self.assertEqual(sealed, plain)
        self.assertIn("é " + CANARY, sealed[0], "a character split across sealed chunks is whole")
        self.assertEqual(self.api.opened[-1]["stream"], True)
        self.assertEqual(op_requests(self.api)[-1]["query"], {"stream": "1"})
        self.assertNothingInTheClear()

    def test_jobs(self):
        for run in (lambda gd: gd.run_job(OK, "build", "make " + CANARY, env={"CI": CANARY}, cpu=50),
                    lambda gd: gd.jobs(OK), lambda gd: gd.kill_job(OK, "build"), lambda gd: gd.job_logs(OK, "build", tail=10),
                    lambda gd: gd.job_logs(OK, "build")):
            plain, sealed = self.both(run)
            self.assertEqual(sealed, plain)
        self.assertEqual(self.api.opened[-2], {"op": "job_logs", "name": "build", "tail": 10})
        self.assertEqual(op_requests(self.api)[-2]["query"], {}, "tail travels sealed only")
        self.assertNothingInTheClear()

    def test_wait_including_held_answers(self):
        for name, timeout in (("failing", "10m"), ("slow", 0), ("held", None)):
            plain, sealed = self.both(lambda gd: gd.wait_job(OK, name, timeout=timeout))
            self.assertEqual(sealed, plain, name)
        self.assertEqual(self.api.opened[0], {"op": "job_wait", "name": "failing", "timeout_ms": 600000})
        for name, cls in (("held-fail", GaiaDeskError), ("held-gone", OperationFailedError), ("nope", OperationFailedError)):
            errs = []
            for gd in (client(self.plain), client(self.api, e2e="require")):
                with self.assertRaises(cls) as cm:
                    gd.wait_job(OK, name)
                errs.append(cm.exception)
            p, s = errs
            self.assertEqual((s.kind, s.reason, str(s), s.desk), (p.kind, p.reason, str(p), p.desk), name)
            self.assertNotEqual(str(s), PLACEHOLDER)
            if name != "nope":
                self.assertEqual(s.json["error"]["status"], p.json["error"]["status"], "a held failure keeps its status")

    def test_follow_logs(self):
        def run(gd):
            f = gd.follow_job_logs(OK, "build")
            return "".join(t for _, t in f.text()), f.wait(), f.result

        plain, sealed = self.both(run)
        self.assertEqual(sealed, plain)
        self.assertEqual(sealed[0], "one\ntwo\nthree\n")
        self.assertEqual(self.api.opened[-1], {"op": "job_logs", "name": "build", "follow": True})
        lost_plain, lost_sealed = self.both(lambda gd: _follow(gd, "lost"))
        self.assertEqual(lost_sealed, lost_plain)

    def test_stats(self):
        plain, sealed = self.both(lambda gd: gd.stats(OK))
        self.assertEqual(sealed, plain)
        self.assertEqual(self.api.opened[-1], {"op": "stats"})

    def test_upload(self):
        data = (CANARY + "\n").encode() * 9000  # > 48 KiB: several input frames
        plain, sealed = self.both(lambda gd: gd.upload_bytes(data, OK, "/srv/" + CANARY + ".txt"))
        self.assertEqual(sealed, plain)
        self.assertEqual(self.api.uploaded[-1], data)
        self.assertEqual(self.api.opened[-1], {"op": "file_put", "path": "/srv/" + CANARY + ".txt", "size": len(data)})
        last = op_requests(self.api)[-1]
        self.assertEqual((last["query"], last["headers"]["content-type"]), ({}, "application/x-ndjson"))
        empty = client(self.api, e2e="require").upload_bytes(b"", OK, "empty.txt")
        self.assertEqual(empty["direction"], "upload")
        self.assertEqual(self.api.uploaded[-1], b"")
        d = tempfile.mkdtemp()
        local = os.path.join(d, "report.csv")
        with open(local, "wb") as f:
            f.write(data[:1000])
        plain, sealed = self.both(lambda gd: gd.upload(local, OK, "in/"))
        self.assertEqual(sealed, plain)
        self.assertEqual(self.api.uploaded[-1], data[:1000])
        self.assertNothingInTheClear()

    def test_download(self):
        path = "/srv/" + CANARY + ".log"
        plain, sealed = self.both(lambda gd: gd.download_bytes(OK, path))
        self.assertEqual(sealed, plain)
        d = tempfile.mkdtemp()
        r = client(self.api, e2e="require").download(OK, path, d + os.sep)
        with open(r["destination"], "rb") as f:
            self.assertEqual(f.read(), plain)
        self.assertEqual(r["bytes"], len(plain))
        self.assertEqual(self.api.opened[-1], {"op": "file_get", "path": path})
        self.api.server.truncate_download = True
        with self.assertRaises(ConnectionLostError):
            client(self.api, e2e="require").download_bytes(OK, path)
        self.assertNothingInTheClear()

    def test_tokens(self):
        for run in (lambda gd: gd.create_token(OK, name="bot", expires="24h", cwd="/srv"), lambda gd: gd.list_tokens(OK),
                    lambda gd: gd.revoke_token(OK, "9f3a1c2b7d004e11")):
            plain, sealed = self.both(run, key="session-person")
            self.assertEqual(sealed, plain)
        self.assertEqual([o["op"] for o in self.api.opened], ["token_mint", "token_list", "token_revoke"])
        self.assertEqual(self.api.opened[-1]["token"], "9f3a1c2b7d004e11")

    def test_a_desks_error_is_opened(self):
        for run, cls in ((lambda gd: gd.exec(REFUSED, "x"), RefusedError), (lambda gd: gd.kill_job(OK, "nope"), OperationFailedError),
                         (lambda gd: gd.stats(REFUSED), GaiaDeskError)):
            errs = []
            for gd in (client(self.plain), client(self.api, e2e="require")):
                try:
                    run(gd)
                except cls as e:
                    errs.append(e)
            if len(errs) == 2:
                p, s = errs
                self.assertEqual((type(s), s.kind, s.reason, str(s), s.status, s.exit_code), (type(p), p.kind, p.reason, str(p), p.status, p.exit_code))
                self.assertNotIn(PLACEHOLDER, str(s))

    def test_async(self):
        async def go():
            gd = client(self.api, AsyncGaiaDesk, e2e="require")
            r = await gd.exec(OK, "hostname", stdin=CANARY)
            self.assertIn("stdin: " + CANARY, r["stdout"])
            s = await gd.exec_stream(OK, "exit 2")
            self.assertEqual((await s.wait()).exit_code, 2)

        asyncio.run(go())
        self.assertNothingInTheClear()

    def test_answers_that_do_not_open(self):
        self.api.server.tamper_answers = True
        with self.assertRaises(EndToEndError) as cm:
            client(self.api).stats(OK)
        self.assertEqual(cm.exception.reason, "e2e_decrypt_failed")
        self.api.server.tamper_answers = False
        self.api.server.plain_in_stream = True
        s = client(self.api).exec_stream(OK, "x")
        self.assertEqual("".join(t for _, t in s.text()), "", "nothing in the clear is taken from a sealed stream")
        self.assertEqual((s.wait().exit_code, s.result["error"]["kind"]), (255, "protocol"))


def _follow(gd, name):
    f = gd.follow_job_logs(OK, name)
    return "".join(t for _, t in f.text()), f.wait(), f.result


@needs_crypto
class Policy(Base):
    def test_auto_seals_whenever_the_desk_lists_a_key(self):
        client(self.api).exec(OK, "echo " + CANARY)
        self.assertEqual(self.api.opened[-1]["op"], "exec")
        self.assertNothingInTheClear()
        client(self.api).stats(OK)
        gets = [r for r in self.api.requests if r["path"] == "/v1/desks/%s" % OK]
        self.assertEqual(len(gets), 2, "one key lookup per client (cached)")
        gd = client(self.api)
        gd.stats(OK)
        gd.stats(OK)
        self.assertEqual(len([r for r in self.api.requests if r["path"] == "/v1/desks/%s" % OK]), 3)

    def test_off_never_seals(self):
        client(self.api, e2e="off").exec(OK, "echo " + CANARY)
        self.assertEqual(__import__("json").loads(op_requests(self.api)[-1]["body"])["command"], "echo " + CANARY)
        self.assertFalse(any(r["path"] == "/v1/desks/%s" % OK for r in self.api.requests), "off reads no key")

    def test_auto_without_a_key_warns_and_sends_in_the_clear(self):
        self.api.desks[OK]["listed"] = False
        with self.assertWarns(RuntimeWarning) as cm:
            r = client(self.api).stats(OK)
        self.assertIn("no end-to-end key", str(cm.warning))
        self.assertEqual(r["cpus"], 8)
        self.assertEqual(self.api.opened, [])

    def test_require_without_a_key_wakes_then_refuses(self):
        self.api.desks[OK]["listed"] = False
        with self.assertRaises(EndToEndError) as cm:
            client(self.api, e2e="require").exec(OK, "echo " + CANARY)
        self.assertEqual((cm.exception.reason, cm.exception.kind), ("e2e_unavailable", "e2e"))
        self.assertEqual(self.api.wakes, [OK])
        self.assertEqual(op_requests(self.api), [], "nothing was sent")
        self.api.desks[OK]["list_on_wake"] = True
        self.assertEqual(client(self.api, e2e="require").stats(OK)["cpus"], 8)
        self.assertEqual(self.api.opened[-1], {"op": "stats"})

    def test_a_desk_that_requires_it_and_lists_no_key_is_woken(self):
        self.api.desks[OK].update(listed=False, required=True, list_on_wake=True)
        self.assertEqual(client(self.api).stats(OK)["cpus"], 8)
        self.assertEqual((self.api.wakes, self.api.opened), ([OK], [{"op": "stats"}]))

    def test_plaintext_refused_e2e_required_is_sent_again_sealed(self):
        self.api.desks[OK].update(listed=False, required=True, hide_required=True, list_on_refusal=True)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = client(self.api).upload_bytes(CANARY.encode(), OK, "a.txt")
        self.assertEqual(r["direction"], "upload")
        ops = op_requests(self.api)
        self.assertEqual(len(ops), 2)
        self.assertEqual(ops[1]["headers"]["content-type"], "application/x-ndjson")
        self.assertEqual(self.api.uploaded, [CANARY.encode()])

    def test_off_does_not_retry_e2e_required(self):
        self.api.desks[OK].update(required=True)
        with self.assertRaises(RefusedError) as cm:
            client(self.api, e2e="off").stats(OK)
        self.assertEqual((cm.exception.reason, cm.exception.status), ("e2e_required", 409))

    def test_a_rotated_key_is_read_again_once(self):
        gd = client(self.api)
        gd.stats(OK)
        self.api.desks[OK]["secret"] = os.urandom(32)  # rotated (and the old key forgotten)
        self.assertEqual(gd.stats(OK)["cpus"], 8)
        self.assertEqual(len(op_requests(self.api)), 3, "refused e2e_decrypt_failed once, then sealed to the new key")

    def test_pinned_keys(self):
        other = _e2e.b64encode(_e2e.public_key(os.urandom(32)))
        with self.assertRaises(EndToEndError) as cm:
            client(self.api, e2e_keys={OK: other}).exec(OK, "echo " + CANARY)
        self.assertEqual(cm.exception.reason, "e2e_key_mismatch")
        self.assertEqual(op_requests(self.api), [])
        mine = _e2e.b64encode(_e2e.public_key(self.api.desks[OK]["secret"]))
        self.assertEqual(client(self.api, e2e_keys={OK: mine}).stats(OK)["cpus"], 8)
        self.api.desks[OK]["advertise"] = other  # a server handing out its own key
        with self.assertRaises(EndToEndError):
            client(self.api, e2e_keys={OK: mine}).stats(OK)
        with self.assertRaises(RefusedError) as cm:
            client(self.api).stats(OK)  # unpinned: sealed to a key the desk does not hold; read again, retried once, refused
        self.assertEqual(cm.exception.reason, "e2e_decrypt_failed")
        self.api.desks[OK].pop("advertise")
        self.api.desks[OK]["listed"] = False
        self.assertEqual(client(self.api, e2e_keys={OK: mine}, e2e="require").stats(OK)["cpus"], 8, "a pinned key needs no listing")


class WithoutCryptography(Base):
    """The [e2e] extra missing (simulated): plaintext with a warning, unless the desk requires it."""

    def setUp(self):
        super().setUp()
        self._was = _e2e.AVAILABLE
        _e2e.AVAILABLE = False
        self.api.desks[OK] = {"secret": None, "listed": True, "advertise": VECTOR_PUB}

    def tearDown(self):
        _e2e.AVAILABLE = self._was
        super().tearDown()

    def test_auto_falls_back_to_plaintext_with_one_warning(self):
        with self.assertWarns(RuntimeWarning) as cm:
            client(self.api).exec(OK, "hostname")
        self.assertIn('pip install "gaiadesk[e2e]"', str(cm.warning))
        self.assertEqual(__import__("json").loads(op_requests(self.api)[-1]["body"]), {"command": "hostname"})
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            client(self.api).stats(OK)
        self.assertEqual(w, [], "warned once")

    def test_a_desk_that_requires_it_is_an_error_naming_the_extra(self):
        self.api.desks[OK]["required"] = True
        with self.assertRaises(EndToEndError) as cm:
            client(self.api).exec(OK, "echo " + CANARY)
        self.assertIn('pip install "gaiadesk[e2e]"', str(cm.exception))
        self.assertEqual(op_requests(self.api), [])

    def test_require_is_an_error(self):
        with self.assertRaises(EndToEndError):
            client(self.api, e2e="require").stats(OK)
        self.assertEqual(op_requests(self.api), [])

    def test_a_refusal_e2e_required_is_an_error_naming_the_extra(self):
        self.api.desks[OK].update(required=True, hide_required=True)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with self.assertRaises(EndToEndError) as cm:
                client(self.api).stats(OK)
        self.assertIn("gaiadesk[e2e]", str(cm.exception))

    def test_off_and_the_package_import(self):
        self.assertEqual(client(self.api, e2e="off").stats(OK)["cpus"], 8)
        import gaiadesk

        self.assertTrue(hasattr(gaiadesk, "EndToEndError"))


class Options(unittest.TestCase):
    def test_bad_options(self):
        for bad in (dict(e2e="sometimes"), dict(e2e_keys={OK: "short"}), dict(e2e_keys=["x"])):
            with self.assertRaises(UsageError, msg=bad):
                GaiaDesk(api_key="ak_x", **bad)
        for t in (dict(), dict(transport="local")):
            with self.assertRaises(UsageError):
                GaiaDesk(e2e="require", **t)
        GaiaDesk(api_key="ak_x", e2e="off", e2e_keys={OK: VECTOR_PUB})


if __name__ == "__main__":
    unittest.main()
