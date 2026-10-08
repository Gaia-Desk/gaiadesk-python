"""The ``api`` transport: GaiaDesk's hosted HTTPS API (https://api.gaiadesk.net/v1),
with the standard library only (``http.client``). Used when the client is
given an ``api_key``. The answers are the CLI's own JSON shapes (the API's
contract references the same schema) and failures the same error envelope,
so every method returns and raises what it does on the CLI transport.
Operations the API does not serve are a ``UsageError``.

Streams (``exec_stream``, ``follow_job_logs``) are Server-Sent Events, read
on a thread into the same Chunk / Exit / ``result`` shape as gaiadesk-cli's
``--json-stream`` (``_api_stream``).

Desk operations are end-to-end encrypted when the desk publishes a key and the
optional ``cryptography`` package is installed (``e2e=``, ``_api_e2e``): the
server then relays only ciphertext, and every method still returns and raises
exactly what it does in the clear.
"""

from __future__ import annotations

import http.client
import json as _json
import os
import re
import ssl
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote, urlencode, urlsplit

from . import _args as A
from . import _api_e2e as E
from . import _timeouts as T
from ._api_stream import ApiStream, AsyncApiStream, SseEvent, SseParser, connecting, desk_op_exit, network_error  # noqa: F401 (re-exported)
from ._native_args import mem_mb
from .errors import (
    GaiaDeskError,
    OperationFailedError,
    ProtocolError,
    UsageError,
    error_envelope,
    error_for_kind,
    exec_outcome,
)

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
    ``_network_error()`` (what no connection is).

    Every wait on the network is bounded (``_timeouts``): ``response_timeout``
    for an answer to begin, ``idle_timeout`` for each read of its body.
    http.client never sends a request twice, and each request has a connection
    of its own (never a pooled one), so nothing is re-sent behind the SDK's back."""

    transport = "api"
    """Which transport this is (``api``, ``local``, ``lan``): the client's ``backend``."""
    _e2e: Optional[E.E2e] = None
    """End-to-end policy (the ``api`` transport only; the desk's own ``local`` and ``lan`` APIs never seal)."""
    response_timeout: Optional[float] = T.DEFAULT_RESPONSE_TIMEOUT
    """Seconds an answer may take to begin, sending the request included (None: no limit)."""
    idle_timeout: Optional[float] = T.DEFAULT_IDLE_TIMEOUT
    """Seconds one read of an answer's body may wait (None: no limit)."""
    where = "the GaiaDesk API"
    """Who answers, in a timeout's message."""

    def __init__(self, api_key: str, desk_token: Optional[str] = None, base_url: Optional[str] = None, wake: Optional[int] = None,
                 e2e: str = "auto", e2e_keys: Optional[Mapping[str, str]] = None,
                 response_timeout: Optional[float] = T.DEFAULT_RESPONSE_TIMEOUT, idle_timeout: Optional[float] = T.DEFAULT_IDLE_TIMEOUT) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise UsageError("api_key must be a non-empty string", kind="usage")
        self._desk_token = check_desk_token(desk_token)
        if wake is not None and (isinstance(wake, bool) or not isinstance(wake, int) or not 0 <= wake <= 120):
            raise UsageError("wake is whole seconds, 0 to 120", kind="usage")
        self._set_base(base_url or DEFAULT_API_URL, ("http", "https"), "base_url must be an http(s) URL: %r" % (base_url,))
        self._key = api_key.strip()
        self._wake = wake
        self.set_timeouts(response_timeout, idle_timeout)
        mode, pinned = E.check_options(e2e, e2e_keys)
        self._e2e = E.E2e(self, mode, pinned)

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

    def set_timeouts(self, response_timeout: Optional[float], idle_timeout: Optional[float]) -> None:
        """Check and set ``response_timeout`` / ``idle_timeout`` (seconds; None: no limit)."""
        self.response_timeout, self.idle_timeout = T.check(response_timeout, idle_timeout)

    def headers(self) -> Dict[str, str]:
        """Every request's headers: the API key, and the desk token when there is one."""
        h = {"Authorization": "Bearer " + self._key, "User-Agent": "gaiadesk-python"}
        if self._desk_token:
            h["X-GaiaDesk-Desk-Token"] = self._desk_token
        return h

    def _connection(self) -> http.client.HTTPConnection:
        if self._https:
            return http.client.HTTPSConnection(self._host, self._port, timeout=self.response_timeout, context=ssl.create_default_context())
        return http.client.HTTPConnection(self._host, self._port, timeout=self.response_timeout)

    def _network_error(self, e: BaseException, op: str) -> GaiaDeskError:
        """The error for a request that got no (complete) answer."""
        return network_error(e, self.base_url, op)

    def body_error(self, e: BaseException, op: str) -> GaiaDeskError:
        """The error for a read of an answer's body that failed: ``idle_timeout`` ran out, or the connection went."""
        if T.is_timeout(e):
            return T.idle_timed_out(self.where, op, self.idle_timeout)
        return self._network_error(e, op)

    def open(self, method: str, path: str, *, query: Optional[Dict[str, Any]] = None, json: Any = None, body: Any = None,
             length: Optional[int] = None, accept: str = "application/json", headers: Optional[Mapping[str, str]] = None,
             content_type: str = "application/octet-stream", seal: Any = None,
             wake: bool = True) -> Tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        """Send one request; an HTTP failure is raised as the typed error from its envelope (a sealed
        operation's, ``seal``, with the desk's error opened into it). ``wake``: send ``wake_s``."""
        op = "%s %s" % (method, path)
        q = {k: v for k, v in (query or {}).items() if v is not None}
        if self._wake is not None and wake:
            q["wake_s"] = self._wake
        url = self._prefix + path + ("?" + urlencode(q) if q else "")
        h = self.headers()
        h["Accept"] = accept
        h.update(headers or {})
        payload: Any = None
        if json is not None:
            h["Content-Type"] = "application/json"
            payload = _json.dumps(json).encode("utf-8")
        elif body is not None:
            h["Content-Type"] = content_type
            payload = body
            if length is not None:
                h["Content-Length"] = str(length)
        conn = self._connection()
        watch = T.ResponseWatch(conn, self.response_timeout)
        try:
            conn.connect()
            watch.check()
            sock = conn.sock
            connecting(conn)  # a stream being started can now be stopped (kill)
            if sock is not None and self.response_timeout is not None:
                # The watch bounds sending and the wait for headers; the socket's own timeout only backs it up.
                sock.settimeout(self.response_timeout + 5)
            conn.request(method, url, body=payload, headers=h)
            resp = conn.getresponse()
            watch.done()
            # The answer began: from here on every read of its body waits at most idle_timeout.
            # (The socket, not conn.sock: http.client hands it to the response when it will close.)
            if sock is not None:
                sock.settimeout(self.idle_timeout)
        except (OSError, http.client.HTTPException) as e:
            watch.done_quietly()
            conn.close()
            if watch.fired or T.is_timeout(e):
                raise T.response_timed_out(self.where, op, self.response_timeout) from e
            raise self._network_error(e, op) from e
        except BaseException:
            watch.done_quietly()
            conn.close()
            raise
        if resp.status >= 400:
            try:
                data = resp.read()
            except (OSError, http.client.HTTPException):
                data = b""  # the status is the answer; its envelope did not arrive
            finally:
                conn.close()
            if seal is not None:
                data = E.unseal_error_body(data, seal, op, resp.status)
            raise api_error(resp.status, data, resp.headers, op)
        return conn, resp

    def call(self, method: str, path: str, **kw: Any) -> Any:
        """A request answered with JSON."""
        return self.read_json(*self.open(method, path, **kw), op="%s %s" % (method, path))

    def desk_call(self, op: E.DeskOp) -> Any:
        """A desk operation answered with JSON, sealed or not: what the plaintext call answers."""
        conn, resp, seal = E.send(self, op)
        r = self.read_json(conn, resp, op=op.label)
        return r if seal is None else E.unseal_json(r, seal, op.label, resp.status)

    def read_json(self, conn: http.client.HTTPConnection, resp: http.client.HTTPResponse, op: str) -> Any:
        try:
            data = resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise self.body_error(e, op) from e
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
        spec = self.exec_spec(command, stdin, shape)
        r = self.desk_call(E.DeskOp(A.check_desk(desk_id), "exec", {"spec": spec}, "POST", path, json=spec))
        if not isinstance(r, dict) or not isinstance(r.get("exit"), int):
            raise ProtocolError("the GaiaDesk API answered exec without a result", kind="protocol", argv=["POST " + path], json=r)
        return exec_outcome(r, check, r["exit"], "", ["POST " + path])

    def exec_stream(self, desk_id: str, command: A.Command, stdin: Union[None, str, bytes], shape: Mapping[str, Any]) -> ApiStream:
        """``POST /desks/{id}/exec?stream=1``: the ExecEvents as a stream."""
        path = self.desk(desk_id) + "/exec"
        spec = self.exec_spec(command, stdin, shape)
        op = E.DeskOp(A.check_desk(desk_id), "exec", {"spec": spec, "stream": True}, "POST", path, json=spec, query={"stream": 1},
                      sealed_query={"stream": 1}, accept="text/event-stream")
        return ApiStream(op.label, "exec", lambda: self._stream(op, "exec"), body_error=lambda e: self.body_error(e, op.label))

    def _stream(self, op: E.DeskOp, kind: str) -> Tuple[Any, ...]:
        conn, resp, seal = E.send(self, op)
        return conn, resp, (None if seal is None else E.EventMapper(seal, kind, op.desk))

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
        r = self.desk_call(E.DeskOp(A.check_desk(desk_id), "file_put", {"path": remote, "size": length}, "PUT", path,
                                    query={"path": remote}, upload=E.Upload(body, length)))
        failed = r.get("failed") if isinstance(r, dict) else None
        if failed:
            raise OperationFailedError("%d file(s) failed to copy" % len(failed), exit_code=1, argv=["PUT " + path], json=r,
                                       kind="failed", desk=A.check_desk(desk_id))
        return r

    def _get_file(self, desk_id: str, remote: str) -> Tuple[Any, ...]:
        if not isinstance(remote, str) or not remote:
            raise UsageError("a remote path is required", kind="usage")
        return E.send(self, E.DeskOp(A.check_desk(desk_id), "file_get", {"path": remote}, "GET", self.desk(desk_id) + "/files",
                                     query={"path": remote}, accept="application/octet-stream"))

    def _read_sealed_file(self, resp: http.client.HTTPResponse, seal: Any, write: Any) -> Any:
        return E.read_sealed_file(resp, seal, write, "GET files", lambda e: self.body_error(e, "GET files"))

    def download_bytes(self, desk_id: str, remote: str) -> bytes:
        """``GET /desks/{id}/files?path=``: the file's bytes."""
        conn, resp, seal = self._get_file(desk_id, remote)
        try:
            if seal is not None:
                buf = bytearray()
                self._read_sealed_file(resp, seal, buf.extend)
                return bytes(buf)
            return resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise self.body_error(e, "GET files") from e
        finally:
            conn.close()

    def download(self, desk_id: str, remote: str, local: str) -> Any:
        """``GET /desks/{id}/files?path=`` into ``local`` (a folder, or a path ending in a separator, keeps the remote name)."""
        started = time.monotonic()
        conn, resp, seal = self._get_file(desk_id, remote)
        dest = os.path.join(local, _basename(remote)) if local.endswith(("/", os.sep)) or os.path.isdir(local) else local
        # Written beside the destination and renamed into place once complete: a download that
        # fails part-way leaves no partial file (and an existing file as it was).
        part = os.path.join(os.path.dirname(dest), ".%s.%s.gaiadesk-part" % (os.path.basename(dest), uuid.uuid4().hex[:12]))
        n = 0
        try:
            try:
                f = open(part, "xb")
            except OSError as e:
                raise GaiaDeskError("cannot write %s: %s" % (dest, e), kind="local", argv=["download"]) from e
            complete = False
            try:
                with f:
                    if seal is not None:
                        counted = [0]

                        def write(b: bytes) -> None:
                            f.write(b)
                            counted[0] += len(b)

                        self._read_sealed_file(resp, seal, write)
                        n = counted[0]
                    while seal is None:
                        try:
                            chunk = resp.read(1 << 20)
                        except (OSError, http.client.HTTPException) as e:
                            raise self.body_error(e, "GET files") from e
                        if not chunk:
                            break
                        f.write(chunk)
                        n += len(chunk)
                try:
                    os.replace(part, dest)
                except OSError as e:
                    raise GaiaDeskError("cannot write %s: %s" % (dest, e), kind="local", argv=["download"]) from e
                complete = True
            finally:
                if not complete:
                    try:
                        os.unlink(part)
                    except OSError:
                        pass
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
        return self.desk_call(E.DeskOp(A.check_desk(desk_id), "job_start", {"spec": spec}, "POST", self.desk(desk_id) + "/jobs", json=spec))

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
            t = min(API_WAIT_MAX, int(-(-left // 1)))
            r = self.desk_call(E.DeskOp(A.check_desk(desk_id), "job_wait", {"name": name, "timeout_ms": t * 1000}, "GET", path,
                                        query={"timeout": t}))
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
        return self.desk_call(E.DeskOp(A.check_desk(desk_id), "job_list", {}, "GET", self.desk(desk_id) + "/jobs"))

    def kill_job(self, desk_id: str, name: str) -> Any:
        path = "%s/jobs/%s" % (self.desk(desk_id), quote(name, safe=""))
        return self.desk_call(E.DeskOp(A.check_desk(desk_id), "job_kill", {"name": name}, "DELETE", path))

    @staticmethod
    def _logs_request(name: str, tail: Optional[int], follow: bool) -> Dict[str, Any]:
        r: Dict[str, Any] = {"name": name}
        if tail is not None:
            r["tail"] = tail
        if follow:
            r["follow"] = True
        return r

    def job_logs(self, desk_id: str, name: str, tail: Optional[int]) -> Any:
        path = "%s/jobs/%s/logs" % (self.desk(desk_id), quote(name, safe=""))
        return self.desk_call(E.DeskOp(A.check_desk(desk_id), "job_logs", self._logs_request(name, tail, False), "GET", path, query={"tail": tail}))

    def follow_job_logs(self, desk_id: str, name: str, tail: Optional[int]) -> ApiStream:
        path = "%s/jobs/%s/logs" % (self.desk(desk_id), quote(name, safe=""))
        op = E.DeskOp(A.check_desk(desk_id), "job_logs", self._logs_request(name, tail, True), "GET", path, query={"follow": 1, "tail": tail},
                      sealed_query={"follow": 1}, accept="text/event-stream")
        return ApiStream(op.label, "logs", lambda: self._stream(op, "logs"), name, body_error=lambda e: self.body_error(e, op.label))

    def stats(self, desk_id: str) -> Any:
        return self.desk_call(E.DeskOp(A.check_desk(desk_id), "stats", {}, "GET", self.desk(desk_id) + "/stats"))

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
                r = self.desk_call(E.DeskOp(A.check_desk(d), "token_mint", {"spec": mint}, "POST", self.desk(d) + "/tokens", json=mint))
            except GaiaDeskError as e:
                if tokens:
                    e.json = dict(e.json if isinstance(e.json, dict) else {}, tokens=tokens)
                raise
            if not isinstance(r, dict) or not isinstance(r.get("tokens"), list):
                raise ProtocolError("the GaiaDesk API minted no tokens", kind="protocol", argv=["token_mint"], json=r)
            tokens += r["tokens"]
        return {"tokens": tokens}

    def list_tokens(self, desk_id: str) -> Any:
        return self.desk_call(E.DeskOp(A.check_desk(desk_id), "token_list", {}, "GET", self.desk(desk_id) + "/tokens"))

    def revoke_token(self, desk_id: str, name: Optional[str], all_for_desk: bool, account: bool) -> Any:
        if all_for_desk:
            raise not_over_api("revoke_token(all_for_desk=True)", "revoke each token by id (list_tokens), or use the CLI or native transport")
        if account:
            raise not_over_api("revoke_token(account=True)", "the API revokes on the desk; drop account=")
        path = "%s/tokens/%s" % (self.desk(desk_id), quote(name or "", safe=""))
        return self.desk_call(E.DeskOp(A.check_desk(desk_id), "token_revoke", {"token": name or ""}, "DELETE", path))
