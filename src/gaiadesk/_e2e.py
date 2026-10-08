"""End-to-end sealing of desk operations over the hosted API (v1): the caller's
side of GaiaDesk's ``protocol/src/e2e.rs``, so the server relays only ciphertext.

Per operation:

1. an ephemeral X25519 key pair; ``shared = X25519(eph, desk e2e_pub)`` (an
   all-zero result is refused);
2. ``prk = HKDF-SHA256-Extract(salt="gaiadesk desk-op e2e v1", shared)``, and for
   each label ``request``, ``input``, ``event``:
   ``key = HKDF-Expand(prk, label 0x00 eph_pub desk_pub, 32)``;
3. every message XChaCha20-Poly1305 with a random 24-byte nonce and the
   associated data ``"gaiadesk-e2e/v1 <label>" 0x00 desk 0x00 op`` (plus
   ``0x00 seq``, u64 big-endian, for input and events).

The crypto is the optional ``cryptography`` package (``pip install
"gaiadesk[e2e]"``); without it ``AVAILABLE`` is False and nothing here but the
pure helpers works. ``cryptography`` has no XChaCha20-Poly1305, so it is built
the standard way (draft-irtf-cfrg-xchacha-03): HChaCha20 over the key and the
nonce's first 16 bytes, then the IETF ChaCha20-Poly1305 with that subkey and
the nonce ``0000 || nonce[16:24]``. HChaCha20 comes from ``cryptography``'s raw
ChaCha20 block (its keystream block 0 is ``rounds(state) + state``, so the
initial state's words are subtracted back out): no ChaCha rounds are written here.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import serialization

    AVAILABLE = True
except ImportError:  # the [e2e] extra is not installed
    AVAILABLE = False

FEATURE = "desk_op_e2e"
"""What a desk that opens sealed operations lists in ``features``."""
VERSION = 1
HEADER = "GaiaDesk-E2E"
"""The header that carries a sealed request (base64url of its JSON) on GET, DELETE and the upload's PUT."""
FRAMES_CONTENT_TYPE = "application/x-ndjson"
HKDF_SALT = b"gaiadesk desk-op e2e v1"
INPUT_MORE, INPUT_LAST = 0, 1
INPUT_CHUNK = 48 * 1024
"""The most file bytes one sealed input frame carries."""
INSTALL_HINT = 'pip install "gaiadesk[e2e]"'

_SIGMA = b"expand 32-byte k"


class OpenError(Exception):
    """A sealed message did not open: ``reason`` is ``e2e_decrypt_failed`` or ``e2e_malformed``."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ───────────────────────────── pure helpers ─────────────────────────────


def b64encode(b: bytes) -> str:
    """base64url without padding."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64decode(s: str) -> bytes:
    """base64url, padding optional; ValueError for anything else."""
    if not isinstance(s, str) or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=" for c in s):
        raise ValueError("not base64url")
    t = s.rstrip("=")
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


def key32(s: str) -> bytes:
    """A base64url X25519 public key: exactly 32 bytes, else ValueError."""
    k = b64decode(s)
    if len(k) != 32:
        raise ValueError("a key is 32 bytes")
    return k


def associated_data(label: str, desk: str, op: str, seq: Optional[int] = None) -> bytes:
    a = b"gaiadesk-e2e/v1 " + label.encode("ascii") + b"\0" + desk.encode("utf-8") + b"\0" + op.encode("utf-8")
    if seq is not None:
        a += b"\0" + struct.pack(">Q", seq)
    return a


def inner_request(request: Dict[str, Any], now: Optional[int] = None) -> bytes:
    """A request's plaintext: ``{"v":1,"ts":<unix seconds>,"request":{…}}``."""
    return json.dumps({"v": VERSION, "ts": int(time.time()) if now is None else now, "request": request},
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def header_value(envelope: Dict[str, Any]) -> str:
    """The ``GaiaDesk-E2E`` header for a sealed request envelope."""
    return b64encode(json.dumps(envelope, separators=(",", ":")).encode("utf-8"))


def b64_len(n: int) -> int:
    """The length of ``n`` bytes as unpadded base64."""
    return (4 * n + 2) // 3


def input_frames_length(size: int) -> int:
    """Bytes of the NDJSON body that uploads a ``size``-byte file as sealed input frames."""
    total, seq, left = 0, 0, size
    while True:
        n = min(INPUT_CHUNK, left)
        line = '{"seq":%d,"nonce":"%s","ciphertext":"%s"}\n' % (seq, "x" * b64_len(24), "x" * b64_len(1 + n + 16))
        total += len(line)
        left -= n
        seq += 1
        if left <= 0:
            return total


# ───────────────────────────── the primitives ─────────────────────────────


def _need() -> None:
    if not AVAILABLE:
        raise RuntimeError("end-to-end encryption needs the cryptography package: %s" % INSTALL_HINT)


def hchacha20(key: bytes, nonce16: bytes) -> bytes:
    """HChaCha20 (draft-irtf-cfrg-xchacha-03 §2.2) from cryptography's ChaCha20 block function."""
    _need()
    if len(key) != 32 or len(nonce16) != 16:
        raise ValueError("HChaCha20 takes a 32-byte key and a 16-byte nonce")
    block = Cipher(algorithms.ChaCha20(key, nonce16), mode=None).encryptor().update(b"\0" * 64)
    out = struct.unpack("<16I", block)
    sigma = struct.unpack("<4I", _SIGMA)
    n = struct.unpack("<4I", nonce16)
    words = [(out[i] - sigma[i]) & 0xFFFFFFFF for i in range(4)] + [(out[12 + i] - n[i]) & 0xFFFFFFFF for i in range(4)]
    return struct.pack("<8I", *words)


def _ietf(key: bytes, nonce: bytes) -> Tuple[Any, bytes]:
    if len(nonce) != 24:
        raise ValueError("an XChaCha20 nonce is 24 bytes")
    return ChaCha20Poly1305(hchacha20(key, nonce[:16])), b"\0\0\0\0" + nonce[16:]


def xchacha_seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """XChaCha20-Poly1305: ciphertext and its 16-byte tag."""
    aead, n = _ietf(key, nonce)
    return aead.encrypt(n, plaintext, aad)


def xchacha_open(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """XChaCha20-Poly1305; ``OpenError`` when it does not authenticate."""
    from cryptography.exceptions import InvalidTag

    aead, n = _ietf(key, nonce)
    try:
        return aead.decrypt(n, ciphertext, aad)
    except InvalidTag:
        raise OpenError("e2e_decrypt_failed", "a sealed message did not open: it was altered, or is not this operation's") from None


def public_key(secret: bytes) -> bytes:
    _need()
    return X25519PrivateKey.from_private_bytes(secret).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def exchange(secret: bytes, public: bytes) -> bytes:
    """X25519, refusing an all-zero (non-contributory) result."""
    _need()
    try:
        shared = X25519PrivateKey.from_private_bytes(secret).exchange(X25519PublicKey.from_public_bytes(public))
    except ValueError:
        shared = b"\0" * 32  # cryptography refuses a low-order point itself
    if shared == b"\0" * 32:
        raise OpenError("e2e_weak_key", "the desk's end-to-end key is not a usable X25519 key")
    return shared


def derive(shared: bytes, eph_pub: bytes, desk_pub: bytes) -> Dict[str, bytes]:
    """The operation's three keys: HKDF-SHA256(salt, shared) expanded per label."""
    _need()
    return {label: HKDF(algorithm=hashes.SHA256(), length=32, salt=HKDF_SALT,
                        info=label.encode("ascii") + b"\0" + eph_pub + desk_pub).derive(shared)
            for label in ("request", "input", "event")}


# ───────────────────────────── one operation ─────────────────────────────


class Seal:
    """One operation's seal (the caller's side): its sealed request (``envelope``),
    its input frames going up and the desk's events coming back, each in order."""

    def __init__(self, keys: Dict[str, bytes], desk: str, op: str, envelope: Dict[str, Any]) -> None:
        self._keys = keys
        self.desk = desk
        self.op = op
        self.envelope = envelope
        """``{"v", "pub", "nonce", "ciphertext"}``: the body's ``e2e`` member or the header's JSON."""
        self._next_input = 0
        self._next_event = 0

    def header(self) -> str:
        return header_value(self.envelope)

    def seal_input_with(self, nonce: bytes, last: bool, data: bytes) -> Dict[str, Any]:
        seq = self._next_input
        self._next_input += 1
        plain = bytes([INPUT_LAST if last else INPUT_MORE]) + bytes(data)
        ct = xchacha_seal(self._keys["input"], nonce, plain, associated_data("input", self.desk, self.op, seq))
        return {"seq": seq, "nonce": b64encode(nonce), "ciphertext": b64encode(ct)}

    def seal_input(self, last: bool, data: bytes) -> Dict[str, Any]:
        """The next input frame, ``{"seq", "nonce", "ciphertext"}``."""
        return self.seal_input_with(os.urandom(24), last, data)

    def open_event(self, frame: Any) -> bytes:
        """The next event's plaintext: it must be the next in order (a gap, repeat or change fails)."""
        if not isinstance(frame, dict) or not isinstance(frame.get("seq"), int) or isinstance(frame.get("seq"), bool):
            raise OpenError("e2e_malformed", "a sealed event is malformed")
        if frame["seq"] != self._next_event:
            raise OpenError("e2e_decrypt_failed", "a sealed event arrived out of order (expected %d, got %d)" % (self._next_event, frame["seq"]))
        try:
            nonce, ct = b64decode(frame.get("nonce")), b64decode(frame.get("ciphertext"))
        except (ValueError, TypeError):
            raise OpenError("e2e_malformed", "a sealed event is malformed") from None
        if len(nonce) != 24 or len(ct) < 16:
            raise OpenError("e2e_malformed", "a sealed event is malformed")
        plain = xchacha_open(self._keys["event"], nonce, ct, associated_data("event", self.desk, self.op, frame["seq"]))
        self._next_event += 1
        return plain

    def open_event_json(self, frame: Any) -> Dict[str, Any]:
        """The next event as the desk event it carries (``stdout``/``stderr``/``exit``/``error``)."""
        try:
            e = json.loads(self.open_event(frame).decode("utf-8"))
        except ValueError:
            raise OpenError("e2e_malformed", "a sealed event opened to something that is not an event") from None
        if not isinstance(e, dict) or e.get("event") not in ("stdout", "stderr", "exit", "error"):
            raise OpenError("e2e_malformed", "a sealed event opened to something that is not an event")
        return e


def seal_request_with(eph: bytes, nonce: bytes, desk_pub: bytes, desk: str, op: str, plaintext: bytes) -> Seal:
    """Seal ``plaintext`` with a given ephemeral secret and nonce (the test vectors' entry point; never reuse either)."""
    eph_pub = public_key(eph)
    keys = derive(exchange(eph, desk_pub), eph_pub, desk_pub)
    ct = xchacha_seal(keys["request"], nonce, plaintext, associated_data("request", desk, op))
    envelope = {"v": VERSION, "pub": b64encode(eph_pub), "nonce": b64encode(nonce), "ciphertext": b64encode(ct)}
    return Seal(keys, desk, op, envelope)


def seal_request(desk_pub: bytes, desk: str, op: str, request: Dict[str, Any]) -> Seal:
    """Seal desk operation ``request`` (``{"op": op, …}``) to desk ``desk``'s key, now."""
    return seal_request_with(os.urandom(32), os.urandom(24), desk_pub, desk, op, inner_request(request))


def open_frames(seal: Seal, frames: List[Any]) -> List[Dict[str, Any]]:
    """Every event of a JSON answer, opened in order."""
    return [seal.open_event_json(f) for f in frames]
