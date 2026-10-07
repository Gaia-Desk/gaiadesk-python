"""Streaming gaiadesk-cli runs: output as it is produced (sync and asyncio)."""

from __future__ import annotations

import asyncio
import codecs
import queue
import signal as _signal
import subprocess
import sys
import threading
import json
from typing import Any, AsyncIterator, Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple, Union

from ._core import not_found


class Chunk(NamedTuple):
    stream: str  # "stdout" | "stderr"
    data: bytes


class Exit(NamedTuple):
    exit_code: Optional[int]
    """gaiadesk-cli's exit code (negative: killed by that signal, POSIX)."""
    stderr_tail: str
    """The last line gaiadesk-cli wrote on stderr (its reason, when it failed)."""


def _last_line(s: str) -> str:
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    last = lines[-1] if lines else ""
    return last[len("gaiadesk-cli:"):].strip() if last.startswith("gaiadesk-cli:") else last


def _interrupt(proc: "subprocess.Popen[bytes]") -> None:
    """SIGINT (gaiadesk-cli stops the remote side cleanly); on Windows, terminate."""
    try:
        if sys.platform == "win32":
            proc.terminate()
        else:
            proc.send_signal(_signal.SIGINT)
    except (ProcessLookupError, OSError):
        pass


class CliStream:
    """A running gaiadesk-cli. Iterate for ``Chunk``s; ``wait()`` for the ``Exit``."""

    def __init__(
        self,
        command: Sequence[str],
        env: Dict[str, str],
        cwd: Optional[str],
        input: Optional[bytes],
        keep_stdin_open: bool = False,
    ) -> None:
        self.argv = list(command)
        self.result: Optional[Dict[str, Any]] = None
        """How the command ended, as JSON: only an ``exec --json-stream`` stream has it."""
        try:
            self._proc = subprocess.Popen(
                list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd
            )
        except OSError as e:
            raise not_found(command[0], command) from e
        self._q: "queue.Queue[Optional[Chunk]]" = queue.Queue()
        self._tail = ""
        self._open = 2
        self._threads = []
        for name, pipe in (("stdout", self._proc.stdout), ("stderr", self._proc.stderr)):
            t = threading.Thread(target=self._pump, args=(name, pipe), daemon=True)
            t.start()
            self._threads.append(t)
        if input is not None:
            self.write(input)
        if not keep_stdin_open:
            self.end()

    def _pump(self, name: str, pipe) -> None:  # type: ignore[no-untyped-def]
        read = getattr(pipe, "read1", pipe.read)
        while True:
            data = read(65536)
            if not data:
                break
            if name == "stderr":
                self._tail = (self._tail + data.decode("utf-8", "replace"))[-4096:]
            self._q.put(Chunk(name, data))
        try:
            pipe.close()
        except OSError:
            pass
        self._q.put(None)

    def write(self, data) -> None:  # type: ignore[no-untyped-def]
        """Write to gaiadesk-cli's stdin (str is UTF-8 encoded)."""
        if self._proc.stdin is None or self._proc.stdin.closed:
            return
        try:
            self._proc.stdin.write(data.encode("utf-8") if isinstance(data, str) else data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def end(self) -> None:
        """Close gaiadesk-cli's stdin."""
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def kill(self) -> None:
        """Stop it: SIGINT (gaiadesk-cli stops the remote side cleanly); terminate on Windows."""
        _interrupt(self._proc)

    def __iter__(self) -> Iterator[Chunk]:
        while self._open:
            c = self._q.get()
            if c is None:
                self._open -= 1
                continue
            yield c

    def text(self) -> Iterator[Tuple[str, str]]:
        """``(stream, text)`` pairs, UTF-8 decoded per stream."""
        dec = {"stdout": codecs.getincrementaldecoder("utf-8")("replace"), "stderr": codecs.getincrementaldecoder("utf-8")("replace")}
        for c in self:
            t = dec[c.stream].decode(c.data)
            if t:
                yield c.stream, t

    def wait(self, timeout: Optional[float] = None) -> Exit:
        code = self._proc.wait(timeout)
        for t in self._threads:
            t.join(timeout)
        return Exit(code, _last_line(self._tail))


class AsyncCliStream:
    """The asyncio ``CliStream``: ``async for chunk in stream``, ``await stream.wait()``."""

    def __init__(self) -> None:
        self._q: "asyncio.Queue[Optional[Chunk]]" = asyncio.Queue()
        self._tail = ""
        self._open = 2
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._tasks: List["asyncio.Task[None]"] = []
        self.argv: List[str] = []
        self.result: Optional[Dict[str, Any]] = None

    @classmethod
    async def start(
        cls,
        command: Sequence[str],
        env: Dict[str, str],
        cwd: Optional[str],
        input: Optional[bytes],
        keep_stdin_open: bool = False,
    ) -> "AsyncCliStream":
        self = cls()
        self.argv = list(command)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, cwd=cwd
            )
        except OSError as e:
            raise not_found(command[0], command) from e
        for name, pipe in (("stdout", self._proc.stdout), ("stderr", self._proc.stderr)):
            self._tasks.append(asyncio.ensure_future(self._pump(name, pipe)))
        if input is not None:
            await self.write(input)
        if not keep_stdin_open:
            self.end()
        return self

    async def _pump(self, name: str, pipe) -> None:  # type: ignore[no-untyped-def]
        while True:
            data = await pipe.read(65536)
            if not data:
                break
            if name == "stderr":
                self._tail = (self._tail + data.decode("utf-8", "replace"))[-4096:]
            await self._q.put(Chunk(name, data))
        await self._q.put(None)

    async def write(self, data) -> None:  # type: ignore[no-untyped-def]
        assert self._proc is not None
        if self._proc.stdin is None or self._proc.stdin.is_closing():
            return
        try:
            self._proc.stdin.write(data.encode("utf-8") if isinstance(data, str) else data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def end(self) -> None:
        assert self._proc is not None
        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            self._proc.stdin.close()

    def kill(self) -> None:
        assert self._proc is not None
        try:
            if sys.platform == "win32":
                self._proc.terminate()
            else:
                self._proc.send_signal(_signal.SIGINT)
        except (ProcessLookupError, OSError):
            pass

    def __aiter__(self) -> AsyncIterator[Chunk]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Chunk]:
        while self._open:
            c = await self._q.get()
            if c is None:
                self._open -= 1
                continue
            yield c

    async def text(self) -> AsyncIterator[Tuple[str, str]]:
        dec = {"stdout": codecs.getincrementaldecoder("utf-8")("replace"), "stderr": codecs.getincrementaldecoder("utf-8")("replace")}
        async for c in self:
            t = dec[c.stream].decode(c.data)
            if t:
                yield c.stream, t

    async def wait(self) -> Exit:
        assert self._proc is not None
        code = await self._proc.wait()
        # Let the readers deliver what is left, but never hang on a pipe
        # some other process still holds open.
        _, pending = await asyncio.wait(self._tasks, timeout=5)
        for t in pending:
            t.cancel()
        return Exit(code, _last_line(self._tail))


# ───────────────────────────── exec --json-stream ─────────────────────────────


class _Events:
    """Turns ``exec --json-stream``'s lines into Chunks; keeps the last ``exit``/``error`` event.
    Also ``logs --follow --json``'s: ``output`` is stdout, ``end`` / ``interrupted``
    the end, and an error envelope ``{"error": {...}}`` an ``error`` event."""

    def __init__(self) -> None:
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf = ""
        self.result: Optional[Dict[str, Any]] = None

    def feed(self, data: bytes, final: bool = False) -> List[Chunk]:
        self._buf += self._dec.decode(data, final)
        out: List[Chunk] = []
        while "\n" in self._buf or (final and self._buf.strip()):
            if "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
            else:
                line, self._buf = self._buf, ""
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("event")
            if kind in ("stdout", "stderr") and isinstance(ev.get("data"), str):
                out.append(Chunk(kind, ev["data"].encode("utf-8")))
            elif kind == "output" and isinstance(ev.get("data"), str):
                out.append(Chunk("stdout", ev["data"].encode("utf-8")))
            elif kind in ("exit", "error", "end", "interrupted"):
                self.result = ev
            elif kind is None and isinstance(ev.get("error"), dict):
                self.result = {"event": "error", "error": ev["error"]}
        return out


def _error_message(result: Optional[Dict[str, Any]]) -> str:
    e = result.get("error") if result else None
    if isinstance(e, dict) and isinstance(e.get("message"), str):
        return e["message"]
    return ""


class JsonExecStream:
    """``exec --json-stream`` with ``CliStream``'s shape: ``Chunk``s of the
    command's stdout and stderr (UTF-8 text: bytes that are not UTF-8 arrive
    replaced), ``wait()`` for the ``Exit``, and ``result``: the last event,
    ``{"event": "exit", ...ExecExit}`` or ``{"event": "error", "exit", "error"}``.
    gaiadesk-cli's own lines on stderr are not chunks; the last one is
    ``Exit.stderr_tail`` (or the error's message)."""

    def __init__(self, raw: CliStream) -> None:
        self._raw = raw
        self.argv = raw.argv
        self._events = _Events()
        self._pending: List[Chunk] = []
        self._done = False

    @property
    def result(self) -> Optional[Dict[str, Any]]:
        return self._events.result

    def _pull(self) -> Iterator[Chunk]:
        for c in self._raw:
            if c.stream == "stdout":
                for x in self._events.feed(c.data):
                    yield x
        for x in self._events.feed(b"", True):
            yield x
        self._done = True

    def __iter__(self) -> Iterator[Chunk]:
        while self._pending:
            yield self._pending.pop(0)
        if not self._done:
            for c in self._pull():
                yield c

    def text(self) -> Iterator[Tuple[str, str]]:
        """``(stream, text)`` pairs."""
        for c in self:
            yield c.stream, c.data.decode("utf-8", "replace")

    def write(self, data: Union[str, bytes]) -> None:
        self._raw.write(data)

    def end(self) -> None:
        self._raw.end()

    def kill(self) -> None:
        self._raw.kill()

    def wait(self, timeout: Optional[float] = None) -> Exit:
        e = self._raw.wait(timeout)
        if not self._done:  # keep what was not read yet for a later iteration
            self._pending.extend(self._pull())
        return Exit(e.exit_code, _error_message(self.result) or e.stderr_tail)


class AsyncJsonExecStream:
    """``JsonExecStream`` for asyncio (``AsyncCliStream``'s shape)."""

    def __init__(self, raw: AsyncCliStream) -> None:
        self._raw = raw
        self.argv = raw.argv
        self._events = _Events()
        self._pending: List[Chunk] = []
        self._done = False

    @property
    def result(self) -> Optional[Dict[str, Any]]:
        return self._events.result

    async def _pull(self) -> AsyncIterator[Chunk]:
        async for c in self._raw:
            if c.stream == "stdout":
                for x in self._events.feed(c.data):
                    yield x
        for x in self._events.feed(b"", True):
            yield x
        self._done = True

    def __aiter__(self) -> AsyncIterator[Chunk]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Chunk]:
        while self._pending:
            yield self._pending.pop(0)
        if not self._done:
            async for c in self._pull():
                yield c

    async def text(self) -> AsyncIterator[Tuple[str, str]]:
        async for c in self:
            yield c.stream, c.data.decode("utf-8", "replace")

    async def write(self, data: Union[str, bytes]) -> None:
        await self._raw.write(data)

    def end(self) -> None:
        self._raw.end()

    def kill(self) -> None:
        self._raw.kill()

    async def wait(self) -> Exit:
        e = await self._raw.wait()
        if not self._done:
            self._pending.extend([c async for c in self._pull()])
        return Exit(e.exit_code, _error_message(self.result) or e.stderr_tail)
