"""The ``api`` transport: GaiaDesk's hosted HTTPS API (https://api.gaiadesk.net/v1),
with the standard library only (``http.client``). Used when the client is
given an ``api_key``. The answers are the CLI's own JSON shapes (the API's
contract references the same schema) and failures the same error envelope,
so every method returns and raises what it does on the CLI transport.
Operations the API does not serve are a ``UsageError``.

Streams (``exec_stream``, ``follow_job_logs``) are Server-Sent Events, read
on a thread into the same Chunk / Exit / ``result`` shape as gaiadesk-cli's
``--json-stream``; ``AsyncApiStream`` is that for asyncio.
"""

from __future__ import annotations

import asyncio
import codecs
import http.client
import json as _json
import os
import queue
import re
import socket
import ssl
import threading
import time
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union
from urllib.parse import quote, urlencode, urlsplit

from . import _args as A
from ._native_args import mem_mb
from .errors import (
    GaiaDeskError,
    OperationFailedError,
    ProtocolError,
    UnreachableError,
    UsageError,
    error_envelope,
    error_for_kind,
    exec_outcome,
)
from .stream import Chunk, Exit

DEFAULT_API_URL = "https://api.gaiadesk.net/v1"
API_FILE_LIMIT = 256 * 1024 * 1024
"""The most one file may be through the API (larger files go direct, through the CLI or native transport)."""
API_WAIT_MAX = 870
"""The longest one ``GET …/jobs/{name}/wait`` holds, in seconds (the API's ``timeout`` maximum)."""

_UNITS = {"s": 1, "sec": 1, "secs": 1, "m": 60, "min": 60, "mins": 60, "h": 3600, "d": 86400, "w": 604800}


def not_over_api(what: str, hint: str = "use the CLI or native transport (construct GaiaDesk without api_key)") -> UsageError:
    """The UsageError for an operation the API does not serve."""
    return UsageError("%s is not available over the API transport; %s" % (what, hint), kind="usage", argv=[what])


def seconds(v: A.Duration, what: str) -> int:
    """A duration as whole seconds: a number, or ``"90"``, ``"30s"``, ``"10m"``, ``"1h30m"``, ``"7d"``, ``"2w"``."""
    d = A.duration(v, what)
    if d.isdigit():
        return int(d)
    total = 0
    for n, unit in re.findall(r"(\d+)\s*([a-zA-Z]+)", d):
        u = _UNITS.get(unit.lower())
        if u is None:
            raise UsageError("%s: unknown unit in %r" % (what, v), kind="usage")
        total += int(n) * u
    return total


def desk_op_exit(kind: str) -> int:
    """gaiadesk-cli's exit code for a desk operation that failed with this kind."""
    return {"refused": 254, "failed": 1, "interrupted": 130}.get(kind, 255)


def _opt_float(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def api_error(status: int, body: bytes, headers: Mapping[str, str], op: str) -> GaiaDeskError:
    """The typed error for a failed request: its error envelope, else a ProtocolError."""
    text = body.decode("utf-8", "replace")
    try:
        parsed: Any = _json.loads(text)
    except ValueError:
        parsed = None
    header_id = headers.get("X-Request-Id")
    retry_after = _opt_float(headers.get("Retry-After"))
    env = error_envelope(parsed)
    if env is None:
        return ProtocolError("the GaiaDesk API answered %s with HTTP %d and no error envelope" % (op, status), kind="protocol",
                             exit_code=255, argv=[op], json=parsed, stderr=text[:4096], status=status, request_id=header_id,
                             retry_after=retry_after)
    rid = parsed["error"].get("request_id")
    return error_for_kind(env.kind, env.message or "HTTP %d" % status, env.reason, exit_code=desk_op_exit(env.kind), argv=[op],
                          json=parsed, desk=env.desk, status=status, request_id=rid if isinstance(rid, str) else header_id,
                          retry_after=retry_after)


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
    out = {"kind": "unreachable" if e.kind == "network" else e.kind, "message": str(e)}
    if e.reason:
        out["reason"] = e.reason
    return out


class ApiStream:
    """An SSE stream from the API with ``JsonExecStream``'s shape: ``Chunk``s,
    ``text()``, ``wait()`` for the ``Exit``, and ``result`` (the last event:
    ``exit`` / ``error`` for exec, ``end`` / ``interrupted`` / ``error`` for logs)."""

    def __init__(self, op: str, kind: str, start: Callable[[], Tuple[http.client.HTTPConnection, http.client.HTTPResponse]],
                 job_name: str = "") -> None:
        self.argv = [op]
        self.result: Optional[Dict[str, Any]] = None
        self._kind = kind
        self._job = job_name
        self._q: "queue.Queue[Optional[Chunk]]" = queue.Queue()
        self._drained = False
        self._killed = False
        self._conn: Optional[http.client.HTTPConnection] = None
        self._exit = Exit(None, "")
        self._thread = threading.Thread(target=self._pump, args=(start,), daemon=True)
        self._thread.start()

    def _pump(self, start: Callable[[], Tuple[http.client.HTTPConnection, http.client.HTTPResponse]]) -> None:
        try:
            conn, resp = start()
            self._conn = conn
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
                done = self._on(ev)
                if done is not None:
                    return done
        for ev in parser.feed(dec.decode(b"", True)) + parser.end():
            done = self._on(ev)
            if done is not None:
                return done
        what = "command" if self._kind == "exec" else "job"
        message = "the event stream ended before the %s did" % what
        self.result = {"event": "error", "exit": 255, "error": {"kind": "connection_lost", "message": message}}
        return Exit(255, message)

    def _on(self, sse: SseEvent) -> Optional[Exit]:
        o = _object(sse)
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


# ───────────────────────────── the transport ─────────────────────────────


def _basename(p: str) -> str:
    parts = [x for x in re.split(r"[\\/]+", p) if x]
    return parts[-1] if parts else ""


def check_desk_token(desk_token: Optional[str]) -> Optional[str]:
    """``desk_token`` stripped, or the UsageError for one that is not a non-empty string."""
    if desk_token is not None and (not isinstance(desk_token, str) or not desk_token.strip()):
        raise UsageError("desk_token must be a non-empty string (a scoped agent token, gdagt_…)", kind="usage")
    return desk_token.strip() if desk_token else None


class ApiTransport:
    """Each operation as an HTTPS request. Thread-safe: one connection per request.

    The HTTP layer is three methods the ``local`` and ``lan`` transports
    (``_local``) override, the operations being the same: ``headers()`` (the
    credentials), ``_connection()`` (a fresh ``http.client`` connection) and
    ``_network_error()`` (what no connection is)."""

    transport = "api"
    """Which transport this is (``api``, ``local``, ``lan``): the client's ``backend``."""

    def __init__(self, api_key: str, desk_token: Optional[str] = None, base_url: Optional[str] = None, wake: Optional[int] = None,
                 timeout: Optional[float] = None) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise UsageError("api_key must be a non-empty string", kind="usage")
        self._desk_token = check_desk_token(desk_token)
        if wake is not None and (isinstance(wake, bool) or not isinstance(wake, int) or not 0 <= wake <= 120):
            raise UsageError("wake is whole seconds, 0 to 120", kind="usage")
        self._set_base(base_url or DEFAULT_API_URL, ("http", "https"), "base_url must be an http(s) URL: %r" % (base_url,))
        self._key = api_key.strip()
        self._wake = wake
        self._timeout = timeout

    def _set_base(self, url: str, schemes: Tuple[str, ...], bad: str) -> None:
        self.base_url = url.rstrip("/")
        u = urlsplit(self.base_url)
        try:
            port = u.port
        except ValueError:
            port = -1
        if u.scheme not in schemes or not u.hostname or port == -1:
            raise UsageError(bad, kind="usage")
        self._https = u.scheme == "https"
        self._host = u.hostname
        self._port = port
        self._prefix = u.path.rstrip("/")

    # HTTP

    def headers(self) -> Dict[str, str]:
        """Every request's headers: the API key, and the desk token when there is one."""
        h = {"Authorization": "Bearer " + self._key, "User-Agent": "gaiadesk-python"}
        if self._desk_token:
            h["X-GaiaDesk-Desk-Token"] = self._desk_token
        return h

    def _connection(self) -> http.client.HTTPConnection:
        if self._https:
            return http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout, context=ssl.create_default_context())
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def _network_error(self, e: BaseException, op: str) -> GaiaDeskError:
        """The error for a request that got no (complete) answer."""
        return network_error(e, self.base_url, op)

    def open(self, method: str, path: str, *, query: Optional[Dict[str, Any]] = None, json: Any = None, body: Any = None,
             length: Optional[int] = None, accept: str = "application/json") -> Tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        """Send one request; an HTTP failure is raised as the typed error from its envelope."""
        op = "%s %s" % (method, path)
        q = {k: v for k, v in (query or {}).items() if v is not None}
        if self._wake is not None:
            q["wake_s"] = self._wake
        url = self._prefix + path + ("?" + urlencode(q) if q else "")
        h = self.headers()
        h["Accept"] = accept
        payload: Any = None
        if json is not None:
            h["Content-Type"] = "application/json"
            payload = _json.dumps(json).encode("utf-8")
        elif body is not None:
            h["Content-Type"] = "application/octet-stream"
            payload = body
            if length is not None:
                h["Content-Length"] = str(length)
        conn = self._connection()
        try:
            conn.request(method, url, body=payload, headers=h)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise self._network_error(e, op) from e
        if resp.status >= 400:
            try:
                data = resp.read()
            finally:
                conn.close()
            raise api_error(resp.status, data, resp.headers, op)
        return conn, resp

    def call(self, method: str, path: str, **kw: Any) -> Any:
        """A request answered with JSON."""
        op = "%s %s" % (method, path)
        conn, resp = self.open(method, path, **kw)
        try:
            data = resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise self._network_error(e, op) from e
        finally:
            conn.close()
        try:
            return _json.loads(data.decode("utf-8"))
        except ValueError:
            raise ProtocolError("the GaiaDesk API answered %s with something that is not JSON" % op, kind="protocol", argv=[op],
                                status=resp.status, request_id=resp.headers.get("X-Request-Id")) from None

    @staticmethod
    def desk(desk_id: str) -> str:
        return "/desks/" + quote(A.check_desk(desk_id), safe="")

    # operations

    def devices(self, desk_id: Optional[str]) -> Any:
        """``GET /desks``: ``{devices, sources, notes}`` (filtered to ``desk_id`` when given)."""
        r = self.call("GET", "/desks")
        if not isinstance(r, dict) or not isinstance(r.get("devices"), list):
            raise ProtocolError("the GaiaDesk API listed no devices", kind="protocol", argv=["GET /desks"], json=r)
        if desk_id is not None:
            d = A.check_desk(desk_id)
            r = dict(r, devices=[x for x in r["devices"] if isinstance(x, dict) and x.get("desk_id") == d])
        return r

    @staticmethod
    def exec_spec(command: A.Command, stdin: Union[None, str, bytes], shape: Mapping[str, Any]) -> Dict[str, Any]:
        spec: Dict[str, Any] = {"command": command} if isinstance(command, str) else {"argv": list(command)}
        if shape.get("shell") is not None:
            spec["shell"] = A.wire_shell(shape["shell"])
        if shape.get("env") is not None:
            spec["env"] = A.check_env(shape["env"])
        if shape.get("cwd") is not None:
            spec["cwd"] = shape["cwd"]
        if shape.get("timeout") is not None:
            spec["timeout_secs"] = seconds(shape["timeout"], "timeout")
        if stdin is not None:
            spec["stdin"] = stdin if isinstance(stdin, str) else bytes(stdin).decode("utf-8", "replace")
        return spec

    def exec(self, desk_id: str, command: A.Command, stdin: Union[None, str, bytes], check: bool, shape: Mapping[str, Any]) -> Any:
        """``POST /desks/{id}/exec``: the ExecResult (a command that never ran is its typed error)."""
        path = self.desk(desk_id) + "/exec"
        r = self.call("POST", path, json=self.exec_spec(command, stdin, shape))
        if not isinstance(r, dict) or not isinstance(r.get("exit"), int):
            raise ProtocolError("the GaiaDesk API answered exec without a result", kind="protocol", argv=["POST " + path], json=r)
        return exec_outcome(r, check, r["exit"], "", ["POST " + path])

    def exec_stream(self, desk_id: str, command: A.Command, stdin: Union[None, str, bytes], shape: Mapping[str, Any]) -> ApiStream:
        """``POST /desks/{id}/exec?stream=1``: the ExecEvents as a stream."""
        path = self.desk(desk_id) + "/exec"
        spec = self.exec_spec(command, stdin, shape)
        return ApiStream("POST " + path, "exec", lambda: self.open("POST", path, json=spec, query={"stream": 1}, accept="text/event-stream"))

    def upload(self, local: str, desk_id: str, remote: str) -> Any:
        """``PUT /desks/{id}/files?path=``: one local file (at most 256 MB); a ``remote`` ending in ``/`` keeps its name."""
        if os.path.isdir(local):
            raise not_over_api("uploading the folder %s" % local, "the API copies single files; copy folders through the CLI or native transport")
        try:
            size = os.path.getsize(local)
            f = open(local, "rb")
        except OSError as e:
            raise GaiaDeskError("cannot read %s: %s" % (local, e), kind="local", argv=["upload"]) from e
        with f:
            if size > API_FILE_LIMIT:
                raise UsageError("%s is %d bytes; the API takes files up to 256 MB (copy larger ones through the CLI or native transport)"
                                 % (local, size), kind="usage", argv=["upload"])
            target = remote + _basename(local) if remote == "" or remote.endswith(("/", "\\")) else remote
            return self._put(desk_id, target, f, size)

    def upload_bytes(self, data: Union[str, bytes], desk_id: str, remote: str) -> Any:
        """``PUT /desks/{id}/files?path=`` with bytes in memory."""
        if not isinstance(remote, str) or not remote:
            raise UsageError("a remote path is required", kind="usage")
        b = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        if len(b) > API_FILE_LIMIT:
            raise UsageError("the API takes files up to 256 MB", kind="usage", argv=["upload"])
        return self._put(desk_id, remote, b, len(b))

    def _put(self, desk_id: str, remote: str, body: Any, length: int) -> Any:
        path = self.desk(desk_id) + "/files"
        r = self.call("PUT", path, query={"path": remote}, body=body, length=length)
        failed = r.get("failed") if isinstance(r, dict) else None
        if failed:
            raise OperationFailedError("%d file(s) failed to copy" % len(failed), exit_code=1, argv=["PUT " + path], json=r,
                                       kind="failed", desk=A.check_desk(desk_id))
        return r

    def _get_file(self, desk_id: str, remote: str) -> Tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        if not isinstance(remote, str) or not remote:
            raise UsageError("a remote path is required", kind="usage")
        return self.open("GET", self.desk(desk_id) + "/files", query={"path": remote}, accept="application/octet-stream")

    def download_bytes(self, desk_id: str, remote: str) -> bytes:
        """``GET /desks/{id}/files?path=``: the file's bytes."""
        conn, resp = self._get_file(desk_id, remote)
        try:
            return resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise self._network_error(e, "GET files") from e
        finally:
            conn.close()

    def download(self, desk_id: str, remote: str, local: str) -> Any:
        """``GET /desks/{id}/files?path=`` into ``local`` (a folder, or a path ending in a separator, keeps the remote name)."""
        started = time.monotonic()
        conn, resp = self._get_file(desk_id, remote)
        dest = os.path.join(local, _basename(remote)) if local.endswith(("/", os.sep)) or os.path.isdir(local) else local
        n = 0
        try:
            try:
                f = open(dest, "wb")
            except OSError as e:
                raise GaiaDeskError("cannot write %s: %s" % (dest, e), kind="local", argv=["download"]) from e
            with f:
                while True:
                    try:
                        chunk = resp.read(1 << 20)
                    except (OSError, http.client.HTTPException) as e:
                        raise self._network_error(e, "GET files") from e
                    if not chunk:
                        break
                    f.write(chunk)
                    n += len(chunk)
        finally:
            conn.close()
        return {"direction": "download", "desk": A.check_desk(desk_id), "destination": dest, "files": 1, "dirs": 0, "bytes": n,
                "resumed_bytes": 0, "failed": [], "seconds": round(time.monotonic() - started, 3)}

    def run_job(self, desk_id: str, name: str, command: A.Command, limits: Mapping[str, Any]) -> Any:
        """``POST /desks/{id}/jobs`` with a JobSpec: the Job."""
        lim: Dict[str, Any] = {}
        if limits.get("priority") is not None:
            lim["priority"] = limits["priority"]
        if limits.get("cpu") is not None:
            lim["cpu_percent"] = limits["cpu"]
        if limits.get("mem") is not None:
            lim["mem_mb"] = mem_mb(limits["mem"])
        if limits.get("keep_awake") is not None:
            lim["keep_awake"] = limits["keep_awake"]
        spec: Dict[str, Any] = {"name": name, "command": [command] if isinstance(command, str) else list(command), "limits": lim}
        if limits.get("cwd") is not None:
            spec["cwd"] = limits["cwd"]
        if limits.get("shell") is not None:
            spec["shell"] = A.wire_shell(limits["shell"])
        if limits.get("env") is not None:
            spec["env"] = A.check_env(limits["env"])
        return self.call("POST", self.desk(desk_id) + "/jobs", json=spec)

    def wait_job(self, desk_id: str, name: str, timeout: Optional[A.Duration]) -> Any:
        """``GET /desks/{id}/jobs/{name}/wait``: ``{job, timed_out}`` once the job is no longer
        running. One request holds at most :data:`API_WAIT_MAX` seconds, so a longer (or no)
        ``timeout`` asks again until the job ends or the time is up. A held answer
        (``GaiaDesk-Held: 1``, its 200 sent before the outcome) starts with keep-alive spaces and
        is oneOf the result or the error envelope (with ``error.status``, the status it would have
        had): the envelope is raised as its typed error, whatever the 200."""
        path = self.desk(desk_id) + "/jobs/" + quote(A.check_job_name(name), safe="") + "/wait"
        total = None if timeout is None else seconds(timeout, "timeout")
        started = time.monotonic()
        while True:
            left = API_WAIT_MAX if total is None else max(0.0, total - (time.monotonic() - started))
            r = self.call("GET", path, query={"timeout": min(API_WAIT_MAX, int(-(-left // 1)))})
            env = error_envelope(r)
            if env is not None:
                raise error_for_kind(env.kind, env.message or "the wait failed", env.reason, exit_code=desk_op_exit(env.kind),
                                     argv=["GET " + path], json=r, desk=env.desk)
            if not isinstance(r, dict) or not isinstance(r.get("job"), dict) or not isinstance(r.get("timed_out"), bool):
                raise ProtocolError("the GaiaDesk API answered a wait without a job", kind="protocol", argv=["GET " + path], json=r)
            over = total is not None and time.monotonic() - started >= total
            if not r["timed_out"] or over or total == 0:
                return r

    def jobs(self, desk_id: str) -> Any:
        return self.call("GET", self.desk(desk_id) + "/jobs")

    def kill_job(self, desk_id: str, name: str) -> Any:
        return self.call("DELETE", "%s/jobs/%s" % (self.desk(desk_id), quote(name, safe="")))

    def job_logs(self, desk_id: str, name: str, tail: Optional[int]) -> Any:
        return self.call("GET", "%s/jobs/%s/logs" % (self.desk(desk_id), quote(name, safe="")), query={"tail": tail})

    def follow_job_logs(self, desk_id: str, name: str, tail: Optional[int]) -> ApiStream:
        path = "%s/jobs/%s/logs" % (self.desk(desk_id), quote(name, safe=""))
        return ApiStream("GET " + path, "logs", lambda: self.open("GET", path, query={"follow": 1, "tail": tail}, accept="text/event-stream"), name)

    def stats(self, desk_id: str) -> Any:
        return self.call("GET", self.desk(desk_id) + "/stats")

    def create_token(self, desks: Union[str, Sequence[str]], spec: Mapping[str, Any]) -> Any:
        """``POST /desks/{id}/tokens`` with a MintSpec, once per desk: one MintResult with every desk's token.
        If a later desk fails, the error's ``json`` carries the tokens already minted (their secrets are shown once)."""
        if spec.get("out") is not None:
            raise not_over_api("create_token(out=...)", "the API returns the secret; write it to a file yourself, or use the CLI or native transport")
        if not spec.get("name"):
            raise UsageError("create_token needs a name over the API transport", kind="usage")
        mint: Dict[str, Any] = {"name": spec["name"], "expires_secs": seconds(spec.get("expires") or "7d", "expires"),
                                "scopes": list(spec["scopes"]) if spec.get("scopes") else ["exec", "cp", "jobs"]}
        if spec.get("cwd") is not None:
            mint["cwd"] = spec["cwd"]
        if spec.get("low_priv"):
            mint["low_priv"] = True
        tokens: List[Any] = []
        for d in [desks] if isinstance(desks, str) else list(desks):
            try:
                r = self.call("POST", self.desk(d) + "/tokens", json=mint)
            except GaiaDeskError as e:
                if tokens:
                    e.json = dict(e.json if isinstance(e.json, dict) else {}, tokens=tokens)
                raise
            if not isinstance(r, dict) or not isinstance(r.get("tokens"), list):
                raise ProtocolError("the GaiaDesk API minted no tokens", kind="protocol", argv=["token_mint"], json=r)
            tokens += r["tokens"]
        return {"tokens": tokens}

    def list_tokens(self, desk_id: str) -> Any:
        return self.call("GET", self.desk(desk_id) + "/tokens")

    def revoke_token(self, desk_id: str, name: Optional[str], all_for_desk: bool, account: bool) -> Any:
        if all_for_desk:
            raise not_over_api("revoke_token(all_for_desk=True)", "revoke each token by id (list_tokens), or use the CLI or native transport")
        if account:
            raise not_over_api("revoke_token(account=True)", "the API revokes on the desk; drop account=")
        return self.call("DELETE", "%s/tokens/%s" % (self.desk(desk_id), quote(name or "", safe="")))
