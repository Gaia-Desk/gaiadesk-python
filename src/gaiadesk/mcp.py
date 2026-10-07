"""A minimal MCP client for ``gaiadesk-cli mcp`` over stdio: how the SDK
reaches the SCREEN tools (open_session, screenshot, click, ...), which
gaiadesk-cli exposes only through its MCP server.

gaiadesk-cli mcp speaks the stateless MCP revision 2026-07-28: no
``initialize``; every request carries the protocol version and client
capabilities in ``params._meta``.
"""

from __future__ import annotations

import asyncio
import collections
import json
import re
import subprocess
import threading
from typing import AbstractSet, Any, Deque, Dict, List, Optional, Sequence, Set

from ._core import not_found
from .errors import GaiaDeskError, McpError

MCP_PROTOCOL_VERSION = "2026-07-28"


def with_protocol_meta(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Every request's params with the stateless protocol's ``_meta`` fields
    added (a value the caller set wins). The one place the handshake lives."""
    p = dict(params or {})
    meta = {
        "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    meta.update(p.get("_meta") or {})
    p["_meta"] = meta
    return p


_meta = with_protocol_meta


def tool_name_alias(name: str) -> Optional[str]:
    """The other spelling of a GaiaDesk tool name: ``gaiadesk.exec`` <-> ``gaiadesk_exec``.

    (Some model providers allow only ``[A-Za-z0-9_-]`` in function names.)
    None for a name that is not a GaiaDesk tool.
    """
    m = re.match(r"^gaiadesk([._])(.+)$", name)
    if not m:
        return None
    return "gaiadesk" + ("_" if m.group(1) == "." else ".") + m.group(2)


def resolve_tool_name(name: str, advertised: Optional[AbstractSet[str]] = None) -> str:
    """The name to send for ``name``: itself if the server advertises it, else
    its alias if the server advertises that, else itself (the server's error
    then says the tool is unknown). Callers may use either spelling."""
    if advertised is None or name in advertised:
        return name
    alias = tool_name_alias(name)
    return alias if alias is not None and alias in advertised else name


def tool_text(result: Dict[str, Any]) -> str:
    """All text content of a tool result, joined."""
    return "\n".join(c["text"] for c in result.get("content", []) if c.get("type") == "text" and isinstance(c.get("text"), str))


def tool_image(result: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """The first image (``gaiadesk.screenshot``): ``{"mime_type", "base64"}``."""
    for c in result.get("content", []):
        if c.get("type") == "image":
            return {"mime_type": c.get("mimeType", ""), "base64": c.get("data", "")}
    return None


def _result(msg: Dict[str, Any]) -> Dict[str, Any]:
    if "error" in msg and msg["error"]:
        e = msg["error"]
        raise McpError(int(e.get("code", 0)), str(e.get("message", "")), e.get("data"))
    return msg.get("result") or {}


class McpClient:
    """Synchronous client: ``list_tools()``, ``call_tool(name, args)``, ``close()``. One request at a time."""

    def __init__(self, command: Sequence[str], env: Dict[str, str], cwd: Optional[str] = None) -> None:
        try:
            self._proc = subprocess.Popen(
                list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd
            )
        except OSError as e:
            raise not_found(command[0], command) from e
        self._stderr: Deque[str] = collections.deque(maxlen=50)
        threading.Thread(target=self._drain, daemon=True).start()
        self._next = 1
        self._lock = threading.Lock()
        self._tool_names: Optional[Set[str]] = None

    def _drain(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr.append(line.decode("utf-8", "replace").rstrip())

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send one request; return its ``result`` (raises McpError for a JSON-RPC error)."""
        with self._lock:
            if self._proc.poll() is not None or self._proc.stdin is None or self._proc.stdin.closed:
                raise GaiaDeskError("the MCP server is not running")
            rid = self._next
            self._next += 1
            body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": _meta(params)}
            try:
                self._proc.stdin.write((json.dumps(body) + "\n").encode("utf-8"))
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise GaiaDeskError("the MCP server is not running") from e
            assert self._proc.stdout is not None
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    tail = self._stderr[-1] if self._stderr else ""
                    raise GaiaDeskError("gaiadesk-cli mcp exited: %s" % tail, exit_code=self._proc.wait())
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == rid:
                    return _result(msg)

    def discover(self) -> Dict[str, Any]:
        return self.request("server/discover")

    def list_tools(self) -> List[Dict[str, Any]]:
        tools = self.request("tools/list").get("tools", [])
        self._tool_names = {t.get("name") for t in tools if isinstance(t, dict)}
        return tools

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """A tool-level failure is a result with ``isError: True``; protocol errors raise McpError.

        ``name`` may be spelled ``gaiadesk.exec`` or ``gaiadesk_exec``: the
        client sends the spelling the server advertises (it fetches the tool
        list once if ``list_tools()`` has not been called).
        """
        if self._tool_names is None and tool_name_alias(name) is not None:
            try:
                self.list_tools()
            except McpError:
                pass  # no list (e.g. no credential): send the name as given
        r = self.request("tools/call", {"name": resolve_tool_name(name, self._tool_names), "arguments": arguments or {}})
        r.setdefault("content", [])
        r.setdefault("isError", False)
        return r

    def close(self) -> None:
        """Close stdin; the server exits."""
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        for f in (self._proc.stdout, self._proc.stderr):
            try:
                if f:
                    f.close()
            except OSError:
                pass

    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class AsyncMcpClient:
    """The asyncio client. Concurrent requests are matched to replies by id."""

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pending: Dict[int, "asyncio.Future[Dict[str, Any]]"] = {}
        self._next = 1
        self._stderr: Deque[str] = collections.deque(maxlen=50)
        self._tasks: List["asyncio.Task[None]"] = []
        self._closed = False
        self._tool_names: Optional[Set[str]] = None

    @classmethod
    async def start(cls, command: Sequence[str], env: Dict[str, str], cwd: Optional[str] = None) -> "AsyncMcpClient":
        self = cls()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, cwd=cwd
            )
        except OSError as e:
            raise not_found(command[0], command) from e
        self._tasks = [asyncio.ensure_future(self._read()), asyncio.ensure_future(self._drain())]
        return self

    async def _drain(self) -> None:
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            self._stderr.append(line.decode("utf-8", "replace").rstrip())

    async def _read(self) -> None:
        assert self._proc and self._proc.stdout
        async for line in self._proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            fut = self._pending.pop(msg.get("id"), None) if isinstance(msg.get("id"), int) else None
            if fut is not None and not fut.done():
                try:
                    fut.set_result(_result(msg))
                except McpError as e:
                    fut.set_exception(e)
        # EOF: the server is gone. Fail what is pending NOW: Process.wait()
        # can block while our stdin pipe is still open (Python 3.12+).
        self._closed = True
        await asyncio.sleep(0.05)  # let the stderr drain catch the server's last words
        err = GaiaDeskError("gaiadesk-cli mcp exited: %s" % (self._stderr[-1] if self._stderr else ""), exit_code=self._proc.returncode)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        assert self._proc is not None
        if self._closed or self._proc.stdin is None or self._proc.stdin.is_closing():
            raise GaiaDeskError("the MCP server is not running")
        rid = self._next
        self._next += 1
        fut: "asyncio.Future[Dict[str, Any]]" = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": _meta(params)}
        self._proc.stdin.write((json.dumps(body) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        return await fut

    async def discover(self) -> Dict[str, Any]:
        return await self.request("server/discover")

    async def list_tools(self) -> List[Dict[str, Any]]:
        tools = (await self.request("tools/list")).get("tools", [])
        self._tool_names = {t.get("name") for t in tools if isinstance(t, dict)}
        return tools

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """As ``McpClient.call_tool``: either tool-name spelling works."""
        if self._tool_names is None and tool_name_alias(name) is not None:
            try:
                await self.list_tools()
            except McpError:
                pass
        r = await self.request("tools/call", {"name": resolve_tool_name(name, self._tool_names), "arguments": arguments or {}})
        r.setdefault("content", [])
        r.setdefault("isError", False)
        return r

    async def close(self) -> None:
        assert self._proc is not None
        self._closed = True
        if self._proc.stdin and not self._proc.stdin.is_closing():
            self._proc.stdin.close()
        await self._proc.wait()
        # The server has exited; its readers have nothing more to deliver.
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def __aenter__(self) -> "AsyncMcpClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
