"""End-to-end sealing (gaiadesk._e2e): the draft's HChaCha20 and XChaCha20-Poly1305
vectors, GaiaDesk's own vectors (protocol/src/e2e/vectors.json) byte for byte, and
everything that must NOT open: tampering, another desk or operation, frames
reordered, replayed or renumbered. Skipped without the [e2e] extra (cryptography)."""

import json
import os
import sys
import unittest

import helpers

sys.path.insert(0, os.path.join(helpers.HERE, "fixtures"))

from gaiadesk import _e2e as E  # noqa: E402

VECTORS = os.path.join(helpers.HERE, "fixtures", "e2e_vectors.json")

with open(VECTORS) as _f:
    V = json.load(_f)


class Pure(unittest.TestCase):
    """What works without cryptography."""

    def test_associated_data_and_base64url(self):
        self.assertEqual(E.associated_data("request", V["desk_id"], V["op"]).hex(), V["aad_request_hex"])
        self.assertEqual(E.associated_data("event", V["desk_id"], V["op"], 1).hex(), V["aad_event_1_hex"])
        self.assertEqual(E.b64encode(b"\xfb\xff"), "-_8")
        self.assertEqual(E.b64decode("-_8"), E.b64decode("-_8="), "padding is tolerated")
        for bad in ("+/8", "a b", "é"):
            self.assertRaises(ValueError, E.b64decode, bad)
        self.assertRaises(ValueError, E.key32, E.b64encode(b"x" * 31))

    def test_header_is_the_vectors(self):
        # The envelope's members in the order the SDK (and the Rust struct) writes them: v, pub, nonce, ciphertext.
        env = {k: V["request"][k] for k in ("v", "pub", "nonce", "ciphertext")}
        self.assertEqual(E.header_value(env), V["request_header"])

    def test_input_body_length_is_exact(self):
        for size in (0, 1, E.INPUT_CHUNK - 1, E.INPUT_CHUNK, E.INPUT_CHUNK + 1, 3 * E.INPUT_CHUNK):
            lines = []
            left, seq = size, 0
            while True:
                n = min(E.INPUT_CHUNK, left)
                left -= n
                lines.append(json.dumps({"seq": seq, "nonce": "n" * 32, "ciphertext": "c" * E.b64_len(n + 17)}, separators=(",", ":")) + "\n")
                seq += 1
                if left == 0:
                    break
            self.assertEqual(E.input_frames_length(size), len("".join(lines)), size)


@unittest.skipUnless(E.AVAILABLE, "needs the cryptography package (gaiadesk[e2e])")
class Vectors(unittest.TestCase):
    def test_hchacha20_draft_vector(self):
        # draft-irtf-cfrg-xchacha-03 §2.2.1
        out = E.hchacha20(bytes(range(32)), bytes.fromhex("000000090000004a0000000031415927"))
        self.assertEqual(out.hex(), "82413b4227b27bfed30e42508a877d73a0f9e4d58a74a853c12ec41326d3ecdc")

    def test_xchacha20_poly1305_draft_vector(self):
        # draft-irtf-cfrg-xchacha-03 §A.3.1
        key, nonce = bytes(range(0x80, 0xA0)), bytes(range(0x40, 0x58))
        aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
        pt = b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it."
        want = ("bd6d179d3e83d43b9576579493c0e939572a1700252bfaccbed2902c21396cbb731c7f1b0b4aa6440bf3a82f4eda7e39ae64c6708c54c216cb96b72e"
                "1213b4522f8c9ba40db5d945b11b69b982c1bb9e3f3fac2bc369488f76b2383565d3fff921f9664c97637da9768812f615c68b13b52ec0875924c1c798"
                "7947deafd8780acf49")
        ct = E.xchacha_seal(key, nonce, pt, aad)
        self.assertEqual(ct.hex(), want)
        self.assertEqual(E.xchacha_open(key, nonce, ct, aad), pt)

    def seal(self):
        return E.seal_request_with(bytes.fromhex(V["eph_secret_hex"]), E.b64decode(V["request"]["nonce"]), E.key32(V["desk_pub"]),
                                   V["desk_id"], V["op"], V["request_plaintext"].encode())

    def test_gaiadesk_vectors_byte_for_byte(self):
        self.assertEqual(E.b64encode(E.public_key(bytes.fromhex(V["desk_secret_hex"]))), V["desk_pub"])
        s = self.seal()
        self.assertEqual(s.envelope, V["request"])
        self.assertEqual(s.header(), V["request_header"])
        for i in V["inputs"]:
            f = s.seal_input_with(E.b64decode(i["nonce"]), i["last"], i["data"].encode())
            self.assertEqual(f, {"seq": i["seq"], "nonce": i["nonce"], "ciphertext": i["ciphertext"]})
        for e in V["events"]:
            self.assertEqual(s.open_event(e).decode(), e["plaintext"])

    def test_the_desk_side_reproduces_the_vectors(self):
        from e2e_desk import open_request

        plain, desk = open_request(bytes.fromhex(V["desk_secret_hex"]), V["desk_id"], V["op"], V["request"])
        self.assertEqual(plain.decode(), V["request_plaintext"])
        for e in V["events"]:
            self.assertEqual(desk.seal_event_with(E.b64decode(e["nonce"]), e["plaintext"].encode())["ciphertext"], e["ciphertext"])
        for i in V["inputs"]:
            self.assertEqual(desk.open_input(i), (i["last"], i["data"].encode()))


@unittest.skipUnless(E.AVAILABLE, "needs the cryptography package (gaiadesk[e2e])")
class WhatMustNotOpen(unittest.TestCase):
    def setUp(self):
        from e2e_desk import open_request

        self.open_request = open_request
        self.secret = os.urandom(32)
        self.pub = E.public_key(self.secret)
        self.caller = E.seal_request(self.pub, "123456789", "exec", {"op": "exec", "spec": {"command": "true"}})
        _, self.desk = open_request(self.secret, "123456789", "exec", self.caller.envelope)

    def flip(self, envelope, field, at=0):
        b = bytearray(E.b64decode(envelope[field]))
        b[at] ^= 0x01
        return dict(envelope, **{field: E.b64encode(bytes(b))})

    def test_round_trip(self):
        inner = json.loads(self.open_request(self.secret, "123456789", "exec", self.caller.envelope)[0])
        self.assertEqual(inner["request"], {"op": "exec", "spec": {"command": "true"}})
        self.assertLess(abs(inner["ts"] - __import__("time").time()), 5)
        f = self.desk.seal_event({"event": "exit", "result": {"exit": 0}})
        self.assertEqual(self.caller.open_event_json(f), {"event": "exit", "result": {"exit": 0}})
        frames = [self.caller.seal_input(False, b"ab"), self.caller.seal_input(True, b"")]
        self.assertEqual([self.desk.open_input(x) for x in frames], [(False, b"ab"), (True, b"")])

    def test_tampered_requests(self):
        env = self.caller.envelope
        for bad in (self.flip(env, "ciphertext"), self.flip(env, "ciphertext", -1), self.flip(env, "nonce"), self.flip(env, "pub")):
            with self.assertRaises(E.OpenError):
                self.open_request(self.secret, "123456789", "exec", bad)

    def test_another_desk_or_operation(self):
        env = self.caller.envelope
        for desk, op in (("123456780", "exec"), ("123456789", "stats"), ("123456789", "job_start")):
            with self.assertRaises(E.OpenError) as cm:
                self.open_request(self.secret, desk, op, env)
            self.assertEqual(cm.exception.reason, "e2e_decrypt_failed")
        with self.assertRaises(E.OpenError):
            self.open_request(os.urandom(32), "123456789", "exec", env)

    def test_events_reordered_replayed_renumbered_or_tampered(self):
        f0 = self.desk.seal_event({"event": "stdout", "data": "YQ=="})
        f1 = self.desk.seal_event({"event": "exit", "result": {}})
        with self.assertRaises(E.OpenError):
            self.caller.open_event(f1)  # reordered: 1 before 0
        self.caller.open_event(f0)
        with self.assertRaises(E.OpenError):
            self.caller.open_event(f0)  # replayed
        with self.assertRaises(E.OpenError):
            self.caller.open_event(dict(f0, seq=1))  # renumbered: 0's ciphertext as 1
        with self.assertRaises(E.OpenError):
            self.caller.open_event(self.flip(f1, "ciphertext"))
        with self.assertRaises(E.OpenError):
            self.caller.open_event(self.flip(f1, "nonce"))
        self.assertEqual(self.caller.open_event_json(f1)["event"], "exit", "a failed frame does not advance the stream")

    def test_inputs_reordered_replayed_renumbered(self):
        a, b = self.caller.seal_input(False, b"a"), self.caller.seal_input(True, b"b")
        with self.assertRaises(E.OpenError):
            self.desk.open_input(b)
        self.desk.open_input(a)
        with self.assertRaises(E.OpenError):
            self.desk.open_input(a)
        with self.assertRaises(E.OpenError):
            self.desk.open_input(dict(a, seq=1))
        self.assertEqual(self.desk.open_input(b), (True, b"b"))

    def test_an_input_frame_is_not_an_event(self):
        x = self.caller.seal_input(True, b"{}")
        with self.assertRaises(E.OpenError):
            self.caller.open_event(x)

    def test_weak_keys_are_refused(self):
        with self.assertRaises(E.OpenError) as cm:
            E.seal_request(b"\0" * 32, "1", "exec", {"op": "exec"})
        self.assertEqual(cm.exception.reason, "e2e_weak_key")

    def test_nonces_and_ephemeral_keys_are_fresh(self):
        a = E.seal_request(self.pub, "1", "stats", {"op": "stats"})
        b = E.seal_request(self.pub, "1", "stats", {"op": "stats"})
        self.assertNotEqual(a.envelope["pub"], b.envelope["pub"])
        self.assertNotEqual(a.envelope["nonce"], b.envelope["nonce"])


if __name__ == "__main__":
    unittest.main()
