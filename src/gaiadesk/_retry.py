"""When the ``api``, ``local`` and ``lan`` transports send a request again: only
when that cannot run anything twice.

* The connection was never made (DNS, refused, a reset during connect or the TLS
  handshake, a socket or pipe that is not there): any method, nothing was sent.
* The connection was lost after sending (closed or reset before any answer), or
  the answer was 502, 503 or 504: GETs only. A 503 saying the API or desk
  operations are switched off is not retried.
* 429 (``rate_limited``, ``desk_busy``) and 409 ``idempotency_key_in_flight``:
  any method, the server refused it before acting.

Never: a timeout, anything once its answer has begun, any other status, and a
call that changes something once it may have reached the server (an
``Idempotency-Key`` does not change that). 429 and 503 wait for ``Retry-After``
(one longer than ``max_retry_wait`` is not waited for: the error carries it);
otherwise exponential backoff with jitter.
"""

from __future__ import annotations

import math
import random
import threading
import time
from typing import Any, Callable, Optional

from .errors import GaiaDeskError, UsageError

DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BASE_DELAY = 0.25
DEFAULT_RETRY_MAX_DELAY = 8.0
DEFAULT_MAX_RETRY_WAIT = 60.0

ANY = "any"
"""A failure after which any method may be sent again (nothing ran)."""
GET = "get"
"""A failure after which only a GET may be sent again (the request may have reached the server)."""
PERMANENT_503 = frozenset({"api_disabled", "desk_ops_disabled", "local_api_off"})

STOP = threading.local()
"""``STOP.event``: set by an ``ApiStream`` being killed, so a backoff wait on its thread ends at once."""


def mark(e: GaiaDeskError, when: Optional[str]) -> GaiaDeskError:
    """``e`` with when it may be retried (``ANY``, ``GET`` or None)."""
    e._retry_when = when  # type: ignore[attr-defined]
    return e


def for_status(e: GaiaDeskError) -> GaiaDeskError:
    """An HTTP failure, marked by its status (and reason)."""
    s, reason = e.status, e.reason
    if s == 429 or (s == 409 and reason == "idempotency_key_in_flight"):
        return mark(e, ANY)
    if s in (502, 504) or (s == 503 and reason not in PERMANENT_503):
        return mark(e, GET)
    return mark(e, None)


def _number(v: Any, name: str, integer: bool = False) -> Any:
    bad = isinstance(v, bool) or not isinstance(v, int if integer else (int, float)) or (not integer and math.isnan(v)) or v < 0
    if bad:
        raise UsageError("%s must be %s, not %r" % (name, "a whole number, 0 or more" if integer else "a number of seconds, 0 or more", v),
                         kind="usage")
    return v if integer else float(v)


class RetryPolicy:
    """``max_retries`` (2: three attempts in all; 0: none), backoff from ``base_delay`` (0.25 s)
    doubling up to ``max_delay`` (8 s) times a random 0.5-1.0, and ``max_wait`` (60 s): the
    longest ``Retry-After`` waited for."""

    def __init__(self, max_retries: int = DEFAULT_MAX_RETRIES, base_delay: float = DEFAULT_RETRY_BASE_DELAY,
                 max_delay: float = DEFAULT_RETRY_MAX_DELAY, max_wait: float = DEFAULT_MAX_RETRY_WAIT,
                 rand: Callable[[float, float], float] = random.uniform) -> None:
        self.max_retries = _number(max_retries, "max_retries", integer=True)
        self.base_delay = _number(base_delay, "retry_base_delay")
        self.max_delay = _number(max_delay, "retry_max_delay")
        self.max_wait = _number(max_wait, "max_retry_wait")
        self._rand = rand

    def delay(self, n: int, retry_after: Optional[float] = None) -> Optional[float]:
        """Seconds to wait before retry ``n`` (0: the first), or None: not waited for (raise now)."""
        if retry_after is not None:
            return None if retry_after > self.max_wait else max(0.0, retry_after)
        return min(self.max_delay, self.base_delay * 2 ** n) * self._rand(0.5, 1.0)

    def run(self, method: str, attempt: Callable[[], Any], rewind: Optional[Callable[[], bool]] = None) -> Any:
        """``attempt()`` until it succeeds or may not be sent again. ``rewind()``: ready the request
        body for another attempt (False: it cannot be, so no retry)."""
        n = 0
        while True:
            try:
                return attempt()
            except GaiaDeskError as e:
                when = getattr(e, "_retry_when", None)
                if n >= self.max_retries or when is None or (when == GET and method != "GET"):
                    raise
                d = self.delay(n, e.retry_after if e.status in (429, 503) else None)
                if d is None or (rewind is not None and not rewind()):
                    raise
                stop = getattr(STOP, "event", None)
                if stop is not None:
                    if stop.wait(d):
                        raise OSError("stopped") from e
                elif d > 0:
                    time.sleep(d)
                n += 1
