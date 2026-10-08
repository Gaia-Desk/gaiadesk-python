"""How long the ``api``, ``local`` and ``lan`` transports wait on the network, so a
server or proxy that stops answering (a dropped connection that is never closed,
a half-open socket, a stalled proxy) is a typed error, never a hang.

* ``response_timeout`` bounds the wait for an answer to begin (its status and
  headers), connecting and sending the request (its body included) counted in.
  A watchdog thread shuts the connection down when it runs out, so the bound
  holds however the time is spent. Exceeded: ``UnreachableError``, kind ``timeout``.
* ``idle_timeout`` bounds every single read of an answer's body (the socket's
  timeout once the headers are in): a per-read limit, not a total, so a large
  download that keeps flowing never times out. Exceeded: ``ConnectionLostError``,
  kind ``timeout``.
"""

from __future__ import annotations

import socket
import threading
from typing import Any, List, Optional, Tuple

from .errors import ConnectionLostError, GaiaDeskError, UnreachableError, UsageError

DEFAULT_RESPONSE_TIMEOUT = 16 * 60.0
"""Seconds: above the API's 15-minute limit on a call (a buffered exec answers only when its command ends)."""
DEFAULT_IDLE_TIMEOUT = 90.0
"""Seconds: the API's streams and held waits send a keep-alive every 15 s."""


def check(response_timeout: Any, idle_timeout: Any) -> Tuple[Optional[float], Optional[float]]:
    """The two timeouts as seconds (None: no limit), or the UsageError for one that is not a positive number."""
    out: List[Optional[float]] = []
    for v, name in ((response_timeout, "response_timeout"), (idle_timeout, "idle_timeout")):
        if v is None:
            out.append(None)
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0 or v != v or v == float("inf"):
            raise UsageError("%s must be a positive number of seconds (or None: no limit), not %r" % (name, v), kind="usage")
        out.append(float(v))
    return out[0], out[1]


def is_timeout(e: BaseException) -> bool:
    """A socket (or pipe) read or write that ran out of time."""
    return isinstance(e, socket.timeout)


def _secs(t: Optional[float]) -> str:
    return ("%g" % t) if t is not None else "unlimited"


def response_timed_out(where: str, op: str, response_timeout: Optional[float]) -> GaiaDeskError:
    return UnreachableError("%s did not answer %s within %s s (response_timeout)" % (where, op, _secs(response_timeout)),
                            kind="timeout", reason="timeout", exit_code=255, argv=[op])


def idle_timed_out(where: str, op: str, idle_timeout: Optional[float]) -> GaiaDeskError:
    return ConnectionLostError("%s stopped sending its answer to %s: nothing for %s s (idle_timeout)" % (where, op, _secs(idle_timeout)),
                               kind="timeout", reason="timeout", exit_code=255, argv=[op])


class ResponseWatch:
    """The ``response_timeout`` of one request: started before connecting, ``done()`` once the
    headers are in. If it runs out first it shuts the connection's socket down, which ends a
    connect, send or receive blocked on it (``fired`` then says the error is a timeout)."""

    def __init__(self, conn: Any, seconds: Optional[float]) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._finished = False
        self.fired = False
        self._timer: Optional[threading.Timer] = None
        if seconds is not None:
            self._timer = threading.Timer(seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if self._finished:
                return
            self.fired = True
        sock = getattr(self._conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def check(self) -> None:
        """Raise ``socket.timeout`` if the time ran out (the connection may have survived it)."""
        if self.fired:
            raise socket.timeout("response_timeout")

    def done_quietly(self) -> None:
        """Stop the watch."""
        with self._lock:
            self._finished = True
        if self._timer is not None:
            self._timer.cancel()

    def done(self) -> None:
        """The answer began: stop the watch (``socket.timeout`` if it already fired)."""
        self.done_quietly()
        self.check()
