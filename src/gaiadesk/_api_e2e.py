"""End-to-end encrypted desk operations on the ``api`` transport (docs/api/README.md,
"End-to-end encryption"): deciding whether to seal (``E2e``: the ``e2e`` option, the
desk's published key, pinned keys, the optional ``cryptography`` package), sending an
operation sealed or in the clear (``send``, with the two retries the contract allows),
and reading a sealed answer back into exactly what the plaintext call answers: a JSON
result or error envelope (``unseal_json``, ``unseal_error_body``), a stream's events
(``EventMapper``: the server's ``ExecEvents`` / ``LogEvents``), a download's bytes
(``read_sealed_file``) and an upload's input frames (``input_body``).

The crypto itself is ``_e2e``. The ``local`` and ``lan`` transports never seal: they
talk to the desk itself.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import http.client
import json as _json
import threading
import time
import warnings
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from . import _e2e
from ._api_stream import desk_op_exit
from .errors import ConnectionLostError, EndToEndError, GaiaDeskError, UsageError, error_envelope, error_for_kind

if TYPE_CHECKING:
    from ._api import ApiTransport

MODES = ("auto", "require", "off")
CACHE_SECS = 300.0
"""How long a desk's key (``GET /desks/{id}``) is used before it is read again."""
WAKE_SECS = 30
E2E_REQUIRED = "e2e_required"
"""The reason a desk (or the server for it) refuses a plaintext operation (409 ``refused``)."""

_warned: set = set()
_warned_lock = threading.Lock()


def _warn_once(key: Any, message: str) -> None:
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    warnings.warn(message, RuntimeWarning, stacklevel=4)


def _e2e_error(message: str, reason: str, **kw: Any) -> EndToEndError:
    return EndToEndError(message, kind="e2e", reason=reason, exit_code=255, **kw)


def check_options(mode: Any, keys: Any) -> Tuple[str, Dict[str, bytes]]:
    """``e2e`` and ``e2e_keys`` checked: the mode, and each pinned key decoded."""
    if mode not in MODES:
        raise UsageError("e2e is auto, require or off (not %r)" % (mode,), kind="usage")
    pinned: Dict[str, bytes] = {}
    if keys is not None:
        if not isinstance(keys, Mapping):
            raise UsageError("e2e_keys maps desk ids to their e2e_pub (base64url)", kind="usage")
        for desk, pub in keys.items():
            try:
                pinned[str(desk)] = _e2e.key32(pub)
            except (ValueError, TypeError):
                raise UsageError("e2e_keys[%r] is not a 32-byte base64url X25519 key" % (desk,), kind="usage") from None
    return mode, pinned


class E2e:
    """One transport's end-to-end policy and its cache of desks' keys."""

    def __init__(self, transport: "ApiTransport", mode: str, pinned: Dict[str, bytes]) -> None:
        self._t = transport
        self.mode = mode
        self._pinned = pinned
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def desk_info(self, desk: str, refresh: bool = False) -> Dict[str, Any]:
        """``GET /desks/{id}`` (cached): its ``e2e_pub``, ``e2e_required`` and ``features``."""
        hit = self._cache.get(desk)
        if hit is not None and not refresh and time.monotonic() < hit[0]:
            return hit[1]
        r = self._t.call("GET", self._t.desk(desk), wake=False)
        info = r if isinstance(r, dict) else {}
        self._cache[desk] = (time.monotonic() + CACHE_SECS, info)
        return info

    def _wake(self, desk: str) -> None:
        wait = self._t._wake if self._t._wake is not None else WAKE_SECS
        try:
            self._t.call("POST", self._t.desk(desk) + "/wake", json={"wait_s": wait}, wake=False)
        except GaiaDeskError:
            pass  # already online, nothing to ring, no desks:write: the key is read again either way

    def _server_key(self, desk: str, info: Dict[str, Any]) -> Optional[bytes]:
        pub = info.get("e2e_pub")
        if not isinstance(pub, str) or not pub:
            return None
        try:
            key = _e2e.key32(pub)
        except ValueError:
            raise _e2e_error("desk %s published an end-to-end key that is not a 32-byte X25519 key" % desk, "e2e_malformed", desk=desk) from None
        pinned = self._pinned.get(desk)
        if pinned is not None and key != pinned:
            raise _e2e_error("the GaiaDesk API handed out an end-to-end key for desk %s that is not the one pinned in e2e_keys; "
                             "nothing was sent" % desk, "e2e_key_mismatch", desk=desk)
        return key

    def key_for(self, desk: str, refresh: bool = False, must: bool = False) -> Optional[bytes]:
        """The key to seal desk ``desk``'s operation to, or None to send it in the clear (with
        a warning where the policy says so). Raises where it must be sealed and cannot be.
        ``must``: the desk refused plaintext (``e2e_required``)."""
        try:
            info = self.desk_info(desk, refresh)
            lookup_error: Optional[GaiaDeskError] = None
        except GaiaDeskError as e:
            info, lookup_error = {}, e
        required = must or self.mode == "require" or info.get("e2e_required") is True
        key = self._server_key(desk, info) or self._pinned.get(desk)
        if not _e2e.AVAILABLE:
            if required:
                raise _e2e_error("desk %s requires end-to-end encryption, which needs the cryptography package: %s"
                                 % (desk, _e2e.INSTALL_HINT), "e2e_unavailable", desk=desk)
            if key is not None:
                _warn_once("crypto", "gaiadesk: the cryptography package is not installed, so desk operations through the API "
                                     "are sent without end-to-end encryption (%s)" % _e2e.INSTALL_HINT)
                return None
        if key is not None:
            return key
        if not required:
            why = "could not be read (%s)" % lookup_error if lookup_error is not None else "publishes no end-to-end key"
            _warn_once(("nokey", desk), "gaiadesk: desk %s %s, so its operations through the API are sent without "
                                        "end-to-end encryption" % (desk, why))
            return None
        self._wake(desk)
        try:
            key = self._server_key(desk, self.desk_info(desk, refresh=True))
        except GaiaDeskError as e:
            if isinstance(e, EndToEndError):
                raise
            raise _e2e_error("the end-to-end key of desk %s could not be read: %s" % (desk, e), "e2e_unavailable", desk=desk) from e
        if key is None:
            raise _e2e_error("desk %s publishes no end-to-end key (it is offline, or its GaiaDesk is too old), and end-to-end "
                             "encryption is required; nothing was sent" % desk, "e2e_unavailable", desk=desk)
        return key


class Upload:
    """A file's bytes going up: ``bytes``, or a binary file of ``size`` bytes (re-readable when seekable)."""

    def __init__(self, source: Any, size: int) -> None:
        self.source = source
        self.size = size
        self._start = source.tell() if hasattr(source, "seek") and hasattr(source, "tell") else None

    def rewind(self) -> bool:
        """Ready to be read again from the start: False when it cannot be (a stream)."""
        if isinstance(self.source, (bytes, bytearray)):
            return True
        try:
            if self._start is None:
                return False
            self.source.seek(self._start)
            return True
        except (OSError, ValueError):
            return False

    def read(self, n: int) -> bytes:
        if isinstance(self.source, (bytes, bytearray)):
            raise TypeError("bytes are not read in pieces")
        return self.source.read(n)


class DeskOp:
    """One desk operation, both ways: in the clear (``query``, ``json``, ``upload`` as the body)
    and sealed (``request``, the sealed request's operation; ``sealed_query``, what stays in the URL)."""

    def __init__(self, desk: str, name: str, request: Dict[str, Any], method: str, path: str, *, query: Optional[Dict[str, Any]] = None,
                 sealed_query: Optional[Dict[str, Any]] = None, json: Any = None, upload: Optional[Upload] = None,
                 accept: str = "application/json") -> None:
        self.desk, self.name, self.request = desk, name, dict(request, op=name)
        self.method, self.path, self.query, self.sealed_query = method, path, query, sealed_query
        self.json, self.upload, self.accept = json, upload, accept

    @property
    def label(self) -> str:
        return "%s %s" % (self.method, self.path)


def input_body(seal: _e2e.Seal, upload: Upload) -> Iterator[bytes]:
    """An upload's NDJSON body: one sealed input frame per line, each at most 48 KiB of the file
    (flag ``1`` on the last, which is empty for an empty file)."""
    left = upload.size
    at = 0
    while True:
        n = min(_e2e.INPUT_CHUNK, left)
        if isinstance(upload.source, (bytes, bytearray)):
            data = bytes(upload.source[at:at + n])
        else:
            data = upload.read(n)
        if len(data) != n:
            raise OSError("the file changed size while it was being sent")
        at += n
        left -= n
        frame = seal.seal_input(left == 0, data)
        yield (_json.dumps(frame, separators=(",", ":")) + "\n").encode("ascii")
        if left == 0:
            return


def _open_sealed(t: "ApiTransport", op: DeskOp, key: bytes) -> Tuple[Any, Any, _e2e.Seal]:
    seal = _e2e.seal_request(key, op.desk, op.name, op.request)
    if op.method == "POST":
        conn, resp = t.open(op.method, op.path, query=op.sealed_query, json={"e2e": seal.envelope}, accept=op.accept, seal=seal,
                            retry=False)
    elif op.upload is not None:
        conn, resp = t.open(op.method, op.path, query=op.sealed_query, headers={_e2e.HEADER: seal.header()},
                            body=input_body(seal, op.upload), length=_e2e.input_frames_length(op.upload.size),
                            content_type=_e2e.FRAMES_CONTENT_TYPE, accept=op.accept, seal=seal, retry=False)
    else:
        conn, resp = t.open(op.method, op.path, query=op.sealed_query, headers={_e2e.HEADER: seal.header()}, accept=op.accept, seal=seal,
                            retry=False)
    return conn, resp, seal


def _open_plain(t: "ApiTransport", op: DeskOp) -> Tuple[Any, Any]:
    if op.upload is not None:
        return t.open(op.method, op.path, query=op.query, body=op.upload.source, length=op.upload.size, accept=op.accept)
    return t.open(op.method, op.path, query=op.query, json=op.json, accept=op.accept)


def send(t: "ApiTransport", op: DeskOp) -> Tuple[Any, Any, Optional[_e2e.Seal]]:
    """Send ``op`` sealed when the transport's policy says so, else in the clear:
    ``(connection, response, seal or None)``. A plaintext operation refused ``e2e_required``
    is sent again sealed, once (an upload only when its source can be read again); a sealed
    one refused ``e2e_decrypt_failed`` (the desk's key changed) again to the key read anew, once."""
    e = t._e2e
    if e is None or e.mode == "off":
        return _open_plain(t, op) + (None,)
    key = e.key_for(op.desk)
    if key is None:
        try:
            return _open_plain(t, op) + (None,)
        except GaiaDeskError as err:
            if err.status != 409 or err.reason != E2E_REQUIRED:
                raise
            if op.upload is not None and not op.upload.rewind():
                raise
            key = e.key_for(op.desk, refresh=True, must=True)
    for attempt in (0, 1):
        try:
            # The transport's retries, each attempt sealed afresh (a fresh key pair and nonces).
            return t.retry.run(op.method, lambda: _open_sealed(t, op, key),  # type: ignore[arg-type]
                               op.upload.rewind if op.upload is not None else None)
        except GaiaDeskError as err:
            if attempt or err.reason != "e2e_decrypt_failed" or (op.upload is not None and not op.upload.rewind()):
                raise
            key = e.key_for(op.desk, refresh=True, must=True)
    raise AssertionError("unreachable")


# ───────────────────────────── answers ─────────────────────────────


def _open_all(seal: _e2e.Seal, frames: Any, op: str, status: Optional[int]) -> List[Dict[str, Any]]:
    if not isinstance(frames, list) or not frames:
        raise _e2e_error("the GaiaDesk API answered %s with no sealed events" % op, "e2e_malformed", argv=[op], status=status)
    try:
        return _e2e.open_frames(seal, frames)
    except _e2e.OpenError as x:
        raise _e2e_error("the end-to-end encrypted answer to %s did not open: %s" % (op, x), x.reason, argv=[op], status=status) from None


def _envelope_with(parsed: Dict[str, Any], ev: Dict[str, Any]) -> Dict[str, Any]:
    """The error envelope with the opened error's kind, message and reason in place of the placeholder."""
    e = dict(parsed["error"])
    if isinstance(ev.get("kind"), str):
        e["kind"] = ev["kind"]
    if isinstance(ev.get("message"), str):
        e["message"] = ev["message"]
    if isinstance(ev.get("reason"), str):
        e["reason"] = ev["reason"]
    else:
        e.pop("reason", None)
    return {"error": e}


def _event_error(ev: Dict[str, Any], desk: str, op: str, status: Optional[int]) -> GaiaDeskError:
    kind = ev.get("kind") if isinstance(ev.get("kind"), str) else "failed"
    message = ev.get("message") if isinstance(ev.get("message"), str) else "the desk reported an error"
    reason = ev.get("reason") if isinstance(ev.get("reason"), str) else None
    err = {"kind": kind, "message": message, "desk": desk}
    if reason:
        err["reason"] = reason
    return error_for_kind(kind, message, reason, exit_code=desk_op_exit(kind), argv=[op], json={"error": err}, desk=desk, status=status)


def unseal_json(parsed: Any, seal: _e2e.Seal, op: str, status: Optional[int]) -> Any:
    """A sealed JSON answer as the plaintext call's: the last event's ``result``; an error
    envelope (a held wait's failure) with its real message; a desk error in a 200 raised."""
    if isinstance(parsed, dict) and isinstance(parsed.get("e2e"), dict):
        events = _open_all(seal, parsed["e2e"].get("events"), op, status)
        last = events[-1]
        if error_envelope(parsed) is not None:
            if last["event"] != "error":
                raise _e2e_error("the GaiaDesk API answered %s with an error whose sealed events end otherwise" % op, "e2e_malformed",
                                 argv=[op], status=status)
            return _envelope_with(parsed, last)
        if last["event"] == "exit" and "result" in last:
            return last["result"]
        if last["event"] == "error":
            raise _event_error(last, seal.desk, op, status)
        raise _e2e_error("the sealed answer to %s ended without its result" % op, "e2e_malformed", argv=[op], status=status)
    if error_envelope(parsed) is not None:
        return parsed  # the server's own failure (a held wait whose desk went away): never sealed
    raise _e2e_error("the GaiaDesk API answered the end-to-end encrypted %s in the clear" % op, "e2e_malformed", argv=[op], status=status)


def unseal_error_body(data: bytes, seal: _e2e.Seal, op: str, status: int) -> bytes:
    """An HTTP error's body with the desk's real error opened into its envelope (the server's own errors are as they are)."""
    try:
        parsed = _json.loads(data.decode("utf-8"))
    except ValueError:
        return data
    if not isinstance(parsed, dict) or not isinstance(parsed.get("e2e"), dict) or error_envelope(parsed) is None:
        return data
    events = _open_all(seal, parsed["e2e"].get("events"), op, status)
    if events[-1]["event"] != "error":
        raise _e2e_error("the GaiaDesk API answered %s with an error whose sealed events end otherwise" % op, "e2e_malformed",
                         argv=[op], status=status)
    return _json.dumps(_envelope_with(parsed, events[-1])).encode("utf-8")


def _bytes_of(data: Any) -> Optional[bytes]:
    if not isinstance(data, str):
        return None
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


def _error_object(ev: Dict[str, Any], desk: str) -> Dict[str, Any]:
    e: Dict[str, Any] = {"kind": ev.get("kind"), "message": ev.get("message"), "desk": desk}
    if isinstance(ev.get("reason"), str):
        e["reason"] = ev["reason"]
    return e


class EventMapper:
    """A sealed stream's events as the plaintext stream's (the server's ``ExecEvents`` for
    ``exec``, ``LogEvents`` for ``logs``): text with a UTF-8 carry per stream, ``exit`` as
    ``ExecExit``, the log's ``end`` / ``interrupted``, ``error`` with the CLI's exit code."""

    def __init__(self, seal: _e2e.Seal, kind: str, desk: str) -> None:
        self._seal = seal
        self._kind = kind
        self._desk = desk
        self._carry = {"stdout": codecs.getincrementaldecoder("utf-8")("replace"), "stderr": codecs.getincrementaldecoder("utf-8")("replace")}

    def map(self, data: str) -> List[Dict[str, Any]]:
        try:
            frame = _json.loads(data)
        except ValueError:
            raise _e2e.OpenError("e2e_malformed", "a sealed event is not JSON") from None
        # Its data names the event, as every /v1 SSE event's does: {"event": "sealed", "seq", "nonce", "ciphertext"}.
        if isinstance(frame, dict) and frame.get("event", "sealed") != "sealed":
            raise _e2e.OpenError("e2e_malformed", "a sealed event's data names another event")
        return self.map_event(self._seal.open_event_json(frame))

    def map_event(self, e: Dict[str, Any]) -> List[Dict[str, Any]]:
        name = e["event"]
        if name in ("stdout", "stderr"):
            b = _bytes_of(e.get("data"))
            if b is None:
                return []
            stream = name if self._kind == "exec" else "stdout"
            text = self._carry[stream].decode(b)
            if not text:
                return []
            return [{"event": name if self._kind == "exec" else "output", "data": text}]
        if name == "exit":
            result = e.get("result")
            out: List[Dict[str, Any]] = []
            for stream in ("stdout", "stderr") if self._kind == "exec" else ("stdout",):
                rest = self._carry[stream].decode(b"", True)
                if rest:
                    out.append({"event": stream if self._kind == "exec" else "output", "data": rest})
            if self._kind == "exec":
                o = {k: v for k, v in (result.items() if isinstance(result, dict) else ()) if k not in ("stdout", "stderr", "truncated")}
                o["event"] = "exit"
                out.append(o)
            elif isinstance(result, dict) and result.get("interrupted") is True:
                out.append({"event": "interrupted"})
            else:
                out.append({"event": "end", "job": result.get("job") if isinstance(result, dict) else None})
            return out
        error = _error_object(e, self._desk)
        if self._kind == "exec":
            return [{"event": "error", "exit": 254 if error["kind"] == "refused" else 255, "error": error}]
        return [{"event": "error", "error": error}]


def read_sealed_file(resp: http.client.HTTPResponse, seal: _e2e.Seal, write: Callable[[bytes], Any], op: str,
                     network_error: Callable[[BaseException], GaiaDeskError]) -> Any:
    """A sealed download (NDJSON of sealed events): each ``stdout`` event's bytes to ``write``,
    until ``exit`` (its CopyResult, returned) or ``error`` (raised). Missing its last event, it is incomplete."""
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    if ctype != _e2e.FRAMES_CONTENT_TYPE:
        raise _e2e_error("the GaiaDesk API answered the end-to-end encrypted %s with %s, not sealed events" % (op, ctype or "no type"),
                         "e2e_malformed", argv=[op], status=resp.status)
    while True:
        try:
            line = resp.readline()
        except (OSError, http.client.HTTPException) as x:
            raise network_error(x) from x
        if not line:
            raise ConnectionLostError("the download of %s ended before the file did" % op, kind="connection_lost", exit_code=255, argv=[op])
        if not line.strip():
            continue
        try:
            ev = seal.open_event_json(_json.loads(line.decode("utf-8")))
        except ValueError:
            raise _e2e_error("a sealed event of %s is not JSON" % op, "e2e_malformed", argv=[op]) from None
        except _e2e.OpenError as x:
            raise _e2e_error("the end-to-end encrypted download did not open: %s" % x, x.reason, argv=[op]) from None
        if ev["event"] == "stdout":
            b = _bytes_of(ev.get("data"))
            if b is None:
                raise _e2e_error("a sealed chunk of %s is not base64" % op, "e2e_malformed", argv=[op])
            write(b)
        elif ev["event"] == "exit":
            return ev.get("result")
        elif ev["event"] == "error":
            raise _event_error(ev, seal.desk, op, None)
