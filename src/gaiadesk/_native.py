"""The native backend: GaiaDesk's client library as a prebuilt extension
(``gaiadesk_native``, ``pip install gaiadesk[native]``), used instead of
spawning gaiadesk-cli when it is installed. Same results (the CLI's JSON
shapes), same error classes and kinds. This module is the only place that
knows the extension's surface, which is small and generic:

    NativeClient(options).call(op, args, input) / .call_sync(...)
    .stream(op, args, input, keep_stdin_open) / .stream_sync(...)  -> next()/next_sync(), write, end, stop
    .forward(args) / .forward_sync(args)                           -> listening, stop, wait()/wait_sync()
    .screen(desk_id) / .screen_sync(desk_id)                       -> call()/call_sync(op), close()/close_sync()
    errors: an exception with .kind, .reason, .json (the CLI's error envelope,
    ``{"error": {kind, message, reason?, desk?}}``)

Results are the CLI's ``--json`` shapes (``job_list`` ``{jobs}``,
``job_logs`` ``{job, output}``, ``job_wait`` ``{job, timed_out}``, ``whoami``
``{source, account}``, ...); exec/shell (call and stream) and job_run take an
optional ``cwd``; exec (call and stream) and job_run an ``env`` ``{NAME: value}``,
job_run a ``shell``.
"""

from __future__ import annotations

import codecs
import importlib
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Mapping, NamedTuple, Optional, Tuple

from .errors import (
    GaiaDeskError,
    OperationFailedError,
    ProtocolError,
    error_class,
    error_envelope,
    exec_outcome,
    sdk_kind,
)
from .stream import Chunk, Exit
from ._native_args import (  # noqa: F401  (the plans in _core build their native arguments through here)
    audit,
    desk,
    devices,
    exec_,
    forward,
    job,
    logs,
    measure,
    run_job,
    shell,
    shell_stream,
    token_create,
    token_revoke,
    wait_job,
)

def load_native(importer: Callable[[str], Any] = importlib.import_module) -> Tuple[Any, str]:
    """``(gaiadesk_native module, "")`` when it imports and its binary loads, else ``(None, why)``."""
    try:
        m = importer("gaiadesk_native")
        m.build_info()
        return m, ""
    except Exception as e:  # ImportError, or a binary that does not load here
        return None, str(e)


def exit_for(kind: str) -> int:
    """gaiadesk-cli's exit code for the same failure (``exit_code`` on the error)."""
    return {"refused": 254, "connection_lost": 253, "failed": 1, "interrupted": 130}.get(kind, 255)


def from_native(e: BaseException, op: str) -> GaiaDeskError:
    """The SDK error for a native one: the class from its ``kind`` (the class the
    CLI backend raises), the SDK kind from its finer ``reason`` when it has one,
    and the desk from its error envelope (``.json``)."""
    if isinstance(e, GaiaDeskError):
        return e
    kind = getattr(e, "kind", None)
    if not isinstance(kind, str):
        kind = "protocol"
    data = getattr(e, "json", None)
    env = error_envelope(data)
    reason = getattr(e, "reason", None)
    if not isinstance(reason, str):
        reason = env.reason if env is not None else None
    desk = env.desk if env is not None else None
    if desk is None and isinstance(data, dict) and isinstance(data.get("desk"), str):
        desk = data["desk"]
    err: GaiaDeskError = error_class(kind)(str(e), exit_code=exit_for(kind), kind=sdk_kind(kind, reason), argv=[op], json=data,
                                          reason=reason, desk=desk)
    return err


def native_options(env: Mapping[str, str], cwd: Optional[str]) -> Dict[str, Any]:
    """The credentials gaiadesk-cli would have read from its environment."""
    o: Dict[str, Any] = {}
    for var, key in (("GAIADESK_TOKEN_FILE", "token_file"), ("GAIADESK_CODE", "code"), ("GAIADESK_TOKEN", "account_token"),
                     ("GAIADESK_AGENT_TOKEN", "agent_token"), ("GAIADESK_SERVER", "server"), ("GAIADESK_PERSIST", "persist")):
        if env.get(var):
            o[key] = env[var]
    if cwd is not None:
        o["cwd"] = cwd
    return o


# ───────────────────────────── requests ─────────────────────────────


def _same(v: Any) -> Any:
    return v


class NativeReq(NamedTuple):
    """One native call: the op, its arguments, stdin, and what to do with the result."""

    op: str
    args: Dict[str, Any]
    input: Optional[bytes] = None
    finish: Callable[[Any], Any] = _same


def exec_finish(op: str, check: bool) -> Callable[[Any], Any]:
    def finish(r: Any) -> Any:
        return exec_outcome(r, check, r.get("exit"), "", [op])

    return finish


def cp_finish(op: str) -> Callable[[Any], Any]:
    def finish(r: Any) -> Any:
        failed = r.get("failed") if isinstance(r, dict) else None
        if failed:
            raise OperationFailedError("%d file(s) failed to copy" % len(failed), exit_code=1, argv=[op], json=r, kind="failed")
        return r

    return finish


def field_finish(key: str) -> Callable[[Any], Any]:
    """A result object's one field (``job_logs`` -> ``output``, ``mesh_ip`` -> ``mesh_ip``)."""

    def finish(r: Any) -> Any:
        if isinstance(r, dict) and isinstance(r.get(key), str):
            return r[key]
        raise ProtocolError("the native library returned no %r" % key, kind="protocol", argv=[key], json=r)

    return finish


def version_finish(r: Any) -> str:
    return "gaiadesk-native %s" % r["version"]


def none_finish(_r: Any) -> None:
    return None


# ───────────────────────────── streams ─────────────────────────────


def _error_text(result: Dict[str, Any]) -> str:
    e = result.get("error")
    m = e.get("message") if isinstance(e, dict) else None
    return m if isinstance(m, str) else ""


def _exit_of(result: Dict[str, Any], tail: str) -> Exit:
    code = result.get("exit") if isinstance(result.get("exit"), int) else 0
    return Exit(code, _last_line(tail) or _error_text(result))


def _last_line(s: str) -> str:
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    return lines[-1] if lines else ""


class NativeStream:
    """A native stream with ``CliStream``'s shape: iterate ``Chunk``s, ``wait()`` for the ``Exit``."""

    def __init__(self, raw: Any, op: str) -> None:
        self._raw = raw
        self.argv = [op]
        self._tail = ""
        self._exit: Optional[Exit] = None
        self.result: Optional[Dict[str, Any]] = None
        """How the run ended (an exec's ``ExecExit``), once the stream is over."""

    def __iter__(self) -> Iterator[Chunk]:
        while self._exit is None:
            ev = self._raw.next_sync()
            if ev is None:
                self._exit = self._exit or Exit(0, _last_line(self._tail))
                return
            kind, payload = ev
            if kind == "exit":
                self.result = payload if isinstance(payload, dict) else None
                self._exit = _exit_of(payload, self._tail)
                return
            if kind == "stderr":
                self._tail = (self._tail + payload.decode("utf-8", "replace"))[-4096:]
            yield Chunk(kind, payload)

    def text(self) -> Iterator[Tuple[str, str]]:
        dec = {"stdout": codecs.getincrementaldecoder("utf-8")("replace"), "stderr": codecs.getincrementaldecoder("utf-8")("replace")}
        for c in self:
            t = dec[c.stream].decode(c.data)
            if t:
                yield c.stream, t

    def write(self, data: Any) -> None:
        self._raw.write(data.encode("utf-8") if isinstance(data, str) else bytes(data))

    def end(self) -> None:
        self._raw.end()

    def kill(self) -> None:
        """Stop the remote side."""
        self._raw.stop()

    def wait(self, timeout: Optional[float] = None) -> Exit:
        for _ in self:
            pass
        assert self._exit is not None
        return self._exit


class AsyncNativeStream:
    """``NativeStream`` for asyncio (``AsyncCliStream``'s shape)."""

    def __init__(self, raw: Any, op: str) -> None:
        self._raw = raw
        self.argv = [op]
        self._tail = ""
        self._exit: Optional[Exit] = None
        self.result: Optional[Dict[str, Any]] = None
        """How the run ended (an exec's ``ExecExit``), once the stream is over."""

    def __aiter__(self) -> AsyncIterator[Chunk]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Chunk]:
        while self._exit is None:
            ev = await self._raw.next()
            if ev is None:
                self._exit = Exit(0, _last_line(self._tail))
                return
            kind, payload = ev
            if kind == "exit":
                self.result = payload if isinstance(payload, dict) else None
                self._exit = _exit_of(payload, self._tail)
                return
            if kind == "stderr":
                self._tail = (self._tail + payload.decode("utf-8", "replace"))[-4096:]
            yield Chunk(kind, payload)

    async def text(self) -> AsyncIterator[Tuple[str, str]]:
        dec = {"stdout": codecs.getincrementaldecoder("utf-8")("replace"), "stderr": codecs.getincrementaldecoder("utf-8")("replace")}
        async for c in self:
            t = dec[c.stream].decode(c.data)
            if t:
                yield c.stream, t

    async def write(self, data: Any) -> None:
        self._raw.write(data.encode("utf-8") if isinstance(data, str) else bytes(data))

    def end(self) -> None:
        self._raw.end()

    def kill(self) -> None:
        self._raw.stop()

    async def wait(self) -> Exit:
        async for _ in self:
            pass
        assert self._exit is not None
        return self._exit


def _fwd_exit(r: Dict[str, Any]) -> Exit:
    return Exit(r.get("exit") if isinstance(r.get("exit"), int) else 0, _error_text(r))


class NativeForward:
    """``Forward`` on the native backend."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self.listening: List[Dict[str, Any]] = raw.listening

    def close(self) -> Exit:
        self._raw.stop()
        return _fwd_exit(self._raw.wait_sync())

    def wait(self) -> Exit:
        return _fwd_exit(self._raw.wait_sync())

    def __enter__(self) -> "NativeForward":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class AsyncNativeForward:
    """``AsyncForward`` on the native backend."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self.listening: List[Dict[str, Any]] = raw.listening

    async def close(self) -> Exit:
        self._raw.stop()
        return _fwd_exit(await self._raw.wait())

    async def wait(self) -> Exit:
        return _fwd_exit(await self._raw.wait())

    async def __aenter__(self) -> "AsyncNativeForward":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


# ───────────────────────────── the backend ─────────────────────────────


class NativeBackend:
    """Runs NativeReqs on a ``NativeClient``, sync or async, with the SDK's errors."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def run_sync(self, r: NativeReq) -> Any:
        try:
            out = self.client.call_sync(r.op, r.args, r.input)
        except Exception as e:
            raise from_native(e, r.op) from e
        return r.finish(out)

    async def run_async(self, r: NativeReq) -> Any:
        try:
            out = await self.client.call(r.op, r.args, r.input)
        except Exception as e:
            raise from_native(e, r.op) from e
        return r.finish(out)

    def stream_sync(self, op: str, args: Dict[str, Any], data: Optional[bytes], keep_open: bool) -> NativeStream:
        try:
            return NativeStream(self.client.stream_sync(op, args, data, keep_open), op)
        except Exception as e:
            raise from_native(e, op) from e

    async def stream_async(self, op: str, args: Dict[str, Any], data: Optional[bytes], keep_open: bool) -> AsyncNativeStream:
        try:
            return AsyncNativeStream(await self.client.stream(op, args, data, keep_open), op)
        except Exception as e:
            raise from_native(e, op) from e

    def forward_sync(self, args: Dict[str, Any]) -> NativeForward:
        try:
            return NativeForward(self.client.forward_sync(args))
        except Exception as e:
            raise from_native(e, "forward") from e

    async def forward_async(self, args: Dict[str, Any]) -> AsyncNativeForward:
        try:
            return AsyncNativeForward(await self.client.forward(args))
        except Exception as e:
            raise from_native(e, "forward") from e

    def agent_connect_sync(self, desk_id: str) -> str:
        try:
            s = self.client.screen_sync(desk_id)
            try:
                meta, _png = s.call_sync("screenshot")
            finally:
                s.close_sync()
        except Exception as e:
            raise from_native(e, "agent-connect") from e
        return "agent session open on desk %s: screenshot %sx%s" % (desk_id, meta["width"], meta["height"])

    async def agent_connect_async(self, desk_id: str) -> str:
        try:
            s = await self.client.screen(desk_id)
            try:
                meta, _png = await s.call("screenshot")
            finally:
                await s.close()
        except Exception as e:
            raise from_native(e, "agent-connect") from e
        return "agent session open on desk %s: screenshot %sx%s" % (desk_id, meta["width"], meta["height"])
