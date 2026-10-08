"""The API transport's streams: Server-Sent Events (``exec_stream``, ``follow_job_logs``)
read on a thread into the same Chunk / Exit / ``result`` shape as gaiadesk-cli's
``--json-stream`` (``ApiStream``), and ``AsyncApiStream``, that for asyncio. A sealed
(end-to-end encrypted) stream's ``sealed`` events are opened and mapped to the very
events a plaintext stream carries (``_api_e2e.EventMapper``)."""

from __future__ import annotations

import asyncio
import codecs
import http.client
import json as _json
import queue
import re
import socket
import threading
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, NamedTuple, Optional, Tuple, Union

from .errors import GaiaDeskError, UnreachableError, UsageError, error_envelope
from .stream import Chunk, Exit

def desk_op_exit(kind: str) -> int:
    """gaiadesk-cli's exit code for a desk operation that failed with this kind."""
    return {"refused": 254, "failed": 1, "interrupted": 130}.get(kind, 255)


def network_error(e: BaseException, base_url: str, op: str) -> GaiaDeskError:
    return UnreachableError("the GaiaDesk API could not be reached (%s): %s" % (base_url, e), kind="network", reason="network",
                            exit_code=255, argv=[op])


# ───────────────────────────── SSE ─────────────────────────────


class SseEvent(NamedTuple):
    event: str
    data: str


class SseParser:
    """An incremental ``text/event-stream`` parser (fields, ``:`` comments, blank-line dispatch;
    an event may be split anywhere, even between ``\\r`` and ``\\n``)."""

    _EOL = re.compile(r"\r\n|\r|\n")

    def __init__(self) -> None:
        self._buf = ""
        self._event = ""
        self._data: List[str] = []

    def feed(self, text: str) -> List[SseEvent]:
        self._buf += text
        out: List[SseEvent] = []
        while True:
            m = self._EOL.search(self._buf)
            if not m:
                break
            if m.group() == "\r" and m.start() == len(self._buf) - 1:
                break  # maybe half of \r\n: wait for the next chunk
            line, self._buf = self._buf[: m.start()], self._buf[m.end():]
            ev = self._line(line)
            if ev is not None:
                out.append(ev)
        return out

    def end(self) -> List[SseEvent]:
        """The end of the stream: an event not finished with a blank line is still delivered."""
        out: List[SseEvent] = []
        if self._buf:
            line, self._buf = self._buf.rstrip("\r"), ""
            ev = self._line(line)
            if ev is not None:
                out.append(ev)
        ev = self._line("")
        if ev is not None:
            out.append(ev)
        return out

    def _line(self, line: str) -> Optional[SseEvent]:
        if line == "":
            if not self._data:
                self._event = ""
                return None
            ev = SseEvent(self._event or "message", "\n".join(self._data))
            self._event, self._data = "", []
            return ev
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None


def _object(ev: SseEvent) -> Optional[Dict[str, Any]]:
    """An event's JSON object, with the SSE name as its ``event`` when it has none."""
    try:
        v = _json.loads(ev.data)
    except ValueError:
        return None
    if not isinstance(v, dict):
        return None
    if not isinstance(v.get("event"), str):
        v = dict(v, event=ev.event)
    return v


def _error_object(e: GaiaDeskError) -> Dict[str, Any]:
    env = error_envelope(e.json)
    if env is not None:
        out: Dict[str, Any] = {"kind": env.kind, "message": env.message or str(e)}
        if env.reason:
            out["reason"] = env.reason
        if env.desk:
            out["desk"] = env.desk
        return out
    kind = {"network": "unreachable", "e2e": "protocol"}.get(e.kind, e.kind)
    out = {"kind": kind, "message": str(e)}
    if e.reason:
        out["reason"] = e.reason
    return out


class ApiStream:
    """An SSE stream from the API with ``JsonExecStream``'s shape: ``Chunk``s,
    ``text()``, ``wait()`` for the ``Exit``, and ``result`` (the last event:
    ``exit`` / ``error`` for exec, ``end`` / ``interrupted`` / ``error`` for logs)."""

    def __init__(self, op: str, kind: str, start: Callable[[], Tuple[Any, ...]], job_name: str = "") -> None:
        self.argv = [op]
        self.result: Optional[Dict[str, Any]] = None
        self._kind = kind
        self._job = job_name
        self._q: "queue.Queue[Optional[Chunk]]" = queue.Queue()
        self._drained = False
        self._killed = False
        self._conn: Optional[http.client.HTTPConnection] = None
        self._mapper: Any = None
        self._resp: Optional[http.client.HTTPResponse] = None
        self._exit = Exit(None, "")
        self._thread = threading.Thread(target=self._pump, args=(start,), daemon=True)
        self._thread.start()

    def _pump(self, start: Callable[[], Tuple[Any, ...]]) -> None:
        try:
            # (connection, response) or, for a sealed stream, (…, its EventMapper).
            started = start()
            self._conn, resp = started[0], started[1]
            self._resp = resp
            self._mapper = started[2] if len(started) > 2 else None
            if self._killed:
                raise OSError("stopped")
            self._exit = self._events(resp)
        except Exception as e:  # noqa: BLE001 (an HTTP failure, the network, or kill())
            if self._killed:
                self._exit = Exit(130, "interrupted")
            else:
                err = e if isinstance(e, GaiaDeskError) else network_error(e, "", self.argv[0])
                error = _error_object(err)
                self.result = {"event": "error", "exit": err.exit_code, "error": error}
                self._exit = Exit(err.exit_code, error["message"])
        finally:
            if self._resp is not None:
                self._resp.close()
            if self._conn is not None:
                self._conn.close()
            self._q.put(None)

    def _events(self, resp: http.client.HTTPResponse) -> Exit:
        parser = SseParser()
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        read = getattr(resp, "read1", None)
        while True:
            data = read(65536) if read is not None else resp.readline()
            if self._killed:
                raise OSError("stopped")
            if not data:
                break
            for ev in parser.feed(dec.decode(data)):
                done = self._on_sse(ev)
                if done is not None:
                    return done
        for ev in parser.feed(dec.decode(b"", True)) + parser.end():
            done = self._on_sse(ev)
            if done is not None:
                return done
        what = "command" if self._kind == "exec" else "job"
        message = "the event stream ended before the %s did" % what
        self.result = {"event": "error", "exit": 255, "error": {"kind": "connection_lost", "message": message}}
        return Exit(255, message)

    def _on_sse(self, sse: SseEvent) -> Optional[Exit]:
        """One SSE event. In a sealed stream only ``sealed`` events (opened, in order) and the
        server's own plaintext ``error`` (the desk lost mid-stream) count; anything else in the
        clear, or a sealed event that does not open, ends the stream as a protocol error."""
        if self._mapper is None:
            return self._on(_object(sse))
        if sse.event == "error":
            return self._on(_object(sse))
        try:
            if sse.event != "sealed":
                raise ValueError("the stream sent a %r event in the clear" % sse.event)
            mapped = self._mapper.map(sse.data)
        except Exception as e:  # noqa: BLE001 (an event that did not open, or a plaintext one)
            message = "the end-to-end encrypted stream could not be read: %s" % e
            self.result = {"event": "error", "exit": 255,
                           "error": {"kind": "protocol", "reason": getattr(e, "reason", "e2e_malformed"), "message": message}}
            return Exit(255, message)
        for o in mapped:
            done = self._on(o)
            if done is not None:
                return done
        return None

    def _on(self, o: Optional[Dict[str, Any]]) -> Optional[Exit]:
        if o is None:
            return None
        kind = o.get("event")
        if kind in ("stdout", "stderr") and self._kind == "exec" and isinstance(o.get("data"), str):
            self._q.put(Chunk(kind, o["data"].encode("utf-8")))
        elif kind == "output" and self._kind == "logs" and isinstance(o.get("data"), str):
            self._q.put(Chunk("stdout", o["data"].encode("utf-8")))
        elif kind in ("exit", "error") and self._kind == "exec":
            self.result = o
            e = o.get("error")
            msg = e.get("message") if isinstance(e, dict) and isinstance(e.get("message"), str) else ""
            return Exit(o.get("exit") if isinstance(o.get("exit"), int) else 255, msg)
        elif kind == "end" and self._kind == "logs":
            self.result = o
            job = o.get("job") if isinstance(o.get("job"), dict) else {}
            name = job.get("name") or self._job
            if isinstance(job.get("exit_code"), int):
                return Exit(0, "job %s exited (exit %d)" % (name, job["exit_code"]))
            return Exit(0, "job %s %s" % (name, job.get("state", "ended")))
        elif kind == "interrupted" and self._kind == "logs":
            self.result = o
            return Exit(0, "stopped following; the job goes on")
        elif kind == "error" and self._kind == "logs":
            e = o.get("error") if isinstance(o.get("error"), dict) else {"kind": "protocol", "message": "the desk reported an error"}
            self.result = {"event": "error", "error": e}
            return Exit(desk_op_exit(str(e.get("kind"))), str(e.get("message", "")))
        return None

    def __iter__(self) -> Iterator[Chunk]:
        while not self._drained:
            c = self._q.get()
            if c is None:
                self._drained = True
                return
            yield c

    def next_chunk(self) -> Optional[Chunk]:
        """The next chunk (blocking), or None at the end."""
        if self._drained:
            return None
        c = self._q.get()
        if c is None:
            self._drained = True
        return c

    def text(self) -> Iterator[Tuple[str, str]]:
        """``(stream, text)`` pairs."""
        for c in self:
            yield c.stream, c.data.decode("utf-8", "replace")

    def write(self, data: Union[str, bytes]) -> None:
        raise UsageError("stdin cannot be written to a command over the API transport (give stdin= as text up front)",
                         kind="usage", argv=self.argv)

    def end(self) -> None:
        """stdin is closed from the start over the API."""

    def kill(self) -> None:
        """Stop: closes the request (the server stops the command, or stops following the job)."""
        self._killed = True
        conn = self._conn
        sock = getattr(conn, "sock", None) if conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def wait(self, timeout: Optional[float] = None) -> Exit:
        self._thread.join(timeout)
        return self._exit


class AsyncApiStream:
    """``ApiStream`` for asyncio (``AsyncCliStream``'s shape); the reading happens on a thread."""

    def __init__(self, stream: ApiStream) -> None:
        self._s = stream
        self.argv = stream.argv

    @property
    def result(self) -> Optional[Dict[str, Any]]:
        return self._s.result

    def __aiter__(self) -> AsyncIterator[Chunk]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Chunk]:
        loop = asyncio.get_running_loop()
        while True:
            c = await loop.run_in_executor(None, self._s.next_chunk)
            if c is None:
                return
            yield c

    async def text(self) -> AsyncIterator[Tuple[str, str]]:
        async for c in self:
            yield c.stream, c.data.decode("utf-8", "replace")

    async def write(self, data: Union[str, bytes]) -> None:
        self._s.write(data)

    def end(self) -> None:
        self._s.end()

    def kill(self) -> None:
        self._s.kill()

    async def wait(self) -> Exit:
        return await asyncio.get_running_loop().run_in_executor(None, self._s.wait)
