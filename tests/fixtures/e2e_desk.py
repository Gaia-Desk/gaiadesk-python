"""The DESK's side of an end-to-end encrypted operation, for the tests only (the SDK
is the caller and never opens a request): open a sealed request with the desk's
static secret, seal its events, open the caller's input frames, in order. Built
from the same primitives as gaiadesk._e2e, checked against vectors.json."""

import json
import os

from gaiadesk import _e2e as E


class DeskSeal:
    def __init__(self, keys, desk, op):
        self.keys, self.desk, self.op = keys, desk, op
        self.next_input = 0
        self.next_event = 0

    def seal_event_with(self, nonce, plaintext):
        seq = self.next_event
        self.next_event += 1
        ct = E.xchacha_seal(self.keys["event"], nonce, plaintext, E.associated_data("event", self.desk, self.op, seq))
        return {"seq": seq, "nonce": E.b64encode(nonce), "ciphertext": E.b64encode(ct)}

    def seal_event(self, event):
        """A desk event (``{"event": "stdout", "data": …}``, ``exit``, ``error``) sealed."""
        return self.seal_event_with(os.urandom(24), json.dumps(event).encode("utf-8"))

    def open_input(self, frame):
        """``(last, bytes)`` of the caller's next input frame."""
        if frame.get("seq") != self.next_input:
            raise E.OpenError("e2e_decrypt_failed", "input out of order")
        plain = E.xchacha_open(self.keys["input"], E.b64decode(frame["nonce"]), E.b64decode(frame["ciphertext"]),
                               E.associated_data("input", self.desk, self.op, frame["seq"]))
        self.next_input += 1
        if not plain or plain[0] not in (E.INPUT_MORE, E.INPUT_LAST):
            raise E.OpenError("e2e_malformed", "bad input flag")
        return plain[0] == E.INPUT_LAST, plain[1:]


def open_request(secret, desk, op, envelope):
    """``(plaintext, DeskSeal)`` of a sealed request to this desk as operation ``op``; OpenError if it does not open."""
    if not isinstance(envelope, dict) or envelope.get("v") != 1:
        raise E.OpenError("e2e_malformed", "bad envelope")
    try:
        eph_pub = E.key32(envelope["pub"])
        nonce, ct = E.b64decode(envelope["nonce"]), E.b64decode(envelope["ciphertext"])
    except (KeyError, ValueError, TypeError):
        raise E.OpenError("e2e_malformed", "bad envelope") from None
    if len(nonce) != 24:
        raise E.OpenError("e2e_malformed", "bad nonce")
    desk_pub = E.public_key(secret)
    keys = E.derive(E.exchange(secret, eph_pub), eph_pub, desk_pub)
    plain = E.xchacha_open(keys["request"], nonce, ct, E.associated_data("request", desk, op))
    return plain, DeskSeal(keys, desk, op)
