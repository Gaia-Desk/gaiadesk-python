"""The asyncio client: the same methods as ``GaiaDesk``, awaitable."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Sequence, Union

from . import _args as A
from ._core import Base, Completed, Plan, failure, not_found, parse_json
from .mcp import AsyncMcpClient
from .stream import AsyncCliStream, Exit
from .types import (
    AuditEvent,
    CpSummary,
    DeskStats,
    DeviceRow,
    DevicesResult,
    ExecResult,
    ForwardListening,
    JobInfo,
    MeasureResult,
    MeshStatus,
    TokenCreateResult,
    TokenInfo,
)


class AsyncForward:
    """A running ``gaiadesk-cli forward``. ``listening``: one entry per forward."""

    def __init__(self, stream: AsyncCliStream, listening: List[ForwardListening]) -> None:
        self._stream = stream
        self.listening = listening

    async def close(self) -> Exit:
        self._stream.kill()
        return await self._stream.wait()

    async def wait(self) -> Exit:
        return await self._stream.wait()

    async def __aenter__(self) -> "AsyncForward":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


class AsyncGaiaDesk(Base):
    """``GaiaDesk`` for asyncio. Same parameters; every method is a coroutine
    (``exec_stream`` & co. return an ``AsyncCliStream``)."""

    async def _run(self, plan: Plan) -> Any:
        return plan.finish(await self._complete(plan.args, plan.input))

    async def _complete(self, args: Sequence[str], input: Optional[bytes]) -> Completed:
        cmd = self.cli + list(args)
        try:
            p = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self.environment(), cwd=self.cwd,
            )
        except OSError as e:
            raise not_found(cmd[0], args) from e
        out, err = await p.communicate(input if input is not None else b"")
        return Completed(p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))

    async def _stream(self, args: Sequence[str], input: Optional[bytes] = None, keep_open: bool = False) -> AsyncCliStream:
        return await AsyncCliStream.start(self.cli + list(args), self.environment(), self.cwd, input, keep_open)

    async def raw(self, args: Sequence[str], input: Union[None, str, bytes] = None) -> Completed:
        return await self._complete(list(args), input.encode("utf-8") if isinstance(input, str) else input)

    async def version(self) -> str:
        return await self._run(self._p_version())

    async def devices(self, *, probe: bool = False, desk_id: Optional[str] = None) -> DevicesResult:
        return await self._run(self._p_devices(probe, desk_id))

    async def probe(self, desk_id: str) -> DeviceRow:
        rows = (await self.devices(probe=True, desk_id=desk_id))["devices"]
        for r in rows:
            if r.get("desk_id") == desk_id:
                return r
        if rows:
            return rows[0]
        from .errors import ProtocolError

        raise ProtocolError("devices --probe listed no row for %s" % desk_id, kind="protocol")

    async def exec(self, desk_id: str, command: A.Command, *, stdin: Union[None, str, bytes] = None, check: bool = False,
                   shell: Optional[str] = None, timeout: Optional[A.Duration] = None, connect_timeout: Optional[A.Duration] = None,
                   persist: Optional[A.Duration] = None, verbose: bool = False) -> ExecResult:
        shape = dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist, verbose=verbose)
        return await self._run(self._p_exec(desk_id, command, stdin, check, shape))

    async def exec_stream(self, desk_id: str, command: A.Command, *, stdin: Union[None, str, bytes, bool] = None,
                          shell: Optional[str] = None, timeout: Optional[A.Duration] = None,
                          connect_timeout: Optional[A.Duration] = None, persist: Optional[A.Duration] = None) -> AsyncCliStream:
        a = A.exec_args(desk_id, command, stdin=stdin is not None and stdin is not False, json=False,
                        shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist)
        data = None if stdin is None or isinstance(stdin, bool) else (stdin.encode("utf-8") if isinstance(stdin, str) else stdin)
        return await self._stream(a, data, keep_open=stdin is True)

    async def shell(self, desk_id: str, script: str, *, check: bool = False, shell: Optional[str] = None,
                    timeout: Optional[A.Duration] = None, connect_timeout: Optional[A.Duration] = None,
                    persist: Optional[A.Duration] = None, verbose: bool = False) -> ExecResult:
        shape = dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist, verbose=verbose)
        return await self._run(self._p_shell(desk_id, script, check, shape))

    async def shell_stream(self, desk_id: str, script: Optional[str] = None, *, shell: Optional[str] = None,
                           timeout: Optional[A.Duration] = None, connect_timeout: Optional[A.Duration] = None) -> AsyncCliStream:
        a = A.shell_args(desk_id, json=False, shell=shell, timeout=timeout, connect_timeout=connect_timeout)
        return await self._stream(a, script.encode("utf-8") if script is not None else None, keep_open=script is None)

    async def upload(self, local: str, desk_id: str, remote: str, *, recursive: bool = False) -> CpSummary:
        return await self._run(self._p_cp("upload", desk_id, local, remote, recursive))

    async def download(self, desk_id: str, remote: str, local: str, *, recursive: bool = False) -> CpSummary:
        return await self._run(self._p_cp("download", desk_id, local, remote, recursive))

    async def run_job(self, desk_id: str, name: str, command: A.Command, *, priority: Optional[str] = None,
                      cpu: Optional[int] = None, mem: Union[None, int, str] = None, keep_awake: Optional[bool] = None) -> JobInfo:
        return await self._run(self._p_op(A.run_args(desk_id, name, command, priority=priority, cpu=cpu, mem=mem, keep_awake=keep_awake)))

    async def jobs(self, desk_id: str) -> List[JobInfo]:
        return await self._run(self._p_op(A.ps_args(desk_id)))

    async def kill_job(self, desk_id: str, name: str) -> JobInfo:
        return await self._run(self._p_op(A.kill_args(desk_id, name)))

    async def job_logs(self, desk_id: str, name: str, *, tail: Optional[int] = None) -> str:
        return await self._run(self._p_text(A.logs_args(desk_id, name, tail)))

    async def follow_job_logs(self, desk_id: str, name: str, *, tail: Optional[int] = None) -> AsyncCliStream:
        return await self._stream(A.logs_args(desk_id, name, tail, follow=True))

    async def stats(self, desk_id: str) -> DeskStats:
        return await self._run(self._p_op(A.stats_args(desk_id)))

    async def measure(self, desk_id: str, *, count: Optional[int] = None) -> MeasureResult:
        return await self._run(self._p_op(A.measure_args(desk_id, count), (0, 1)))

    async def create_token(self, desks: Union[str, Sequence[str]], *, name: Optional[str] = None, expires: Optional[str] = None,
                           scopes: Optional[Sequence[str]] = None, cwd: Optional[str] = None, low_priv: bool = False,
                           out: Optional[str] = None) -> TokenCreateResult:
        return await self._run(self._p_op(A.token_create_args(desks, name=name, expires=expires, scopes=scopes, cwd=cwd, low_priv=low_priv, out=out)))

    async def list_tokens(self, desk_id: str) -> List[TokenInfo]:
        return await self._run(self._p_op(A.token_list_args(desk_id)))

    async def revoke_token(self, desk_id: str, name: Optional[str] = None, *, all_for_desk: bool = False,
                           account: bool = False) -> Dict[str, Any]:
        return await self._run(self._p_op(A.token_revoke_args(desk_id, name, all_for_desk, account)))

    async def audit(self, desk_id: str, *, token: Optional[str] = None, limit: Optional[int] = None,
                    account: bool = False) -> List[AuditEvent]:
        return await self._run(self._p_op(A.audit_args(desk_id, token, limit, account)))

    async def mesh_status(self) -> MeshStatus:
        return await self._run(self._p_op(["mesh", "status", "--json"]))

    async def mesh_ip(self, desk_id: str) -> str:
        return await self._run(self._p_mesh_ip(desk_id))

    async def disconnect(self, desk_id: Optional[str] = None) -> None:
        await self._run(self._p_none(A.disconnect_args(desk_id)))

    async def forward(self, desk_id: str, specs: Union[Dict[str, Any], Sequence[Dict[str, Any]]]) -> AsyncForward:
        lst = [specs] if isinstance(specs, dict) else list(specs)
        a = A.forward_args(desk_id, lst)
        s = await self._stream(a)
        listening: List[ForwardListening] = []
        buf = ""
        stderr = ""
        async for c in s:
            if c.stream == "stderr":
                stderr += c.data.decode("utf-8", "replace")
                continue
            buf += c.data.decode("utf-8", "replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                ev = parse_json(line)
                if isinstance(ev, dict) and ev.get("event") == "listening":
                    listening.append(ev)  # type: ignore[arg-type]
            if len(listening) == len(lst):
                return AsyncForward(s, listening)
        e = await s.wait()
        raise failure(Completed(e.exit_code, "", stderr), a, None)

    async def agent_connect(self, desk_id: str) -> str:
        return await self._run(self._p_text(A.agent_connect_args(desk_id, self.server), strip=True))

    async def mcp(self, *, audit_dir: Optional[str] = None, allow_domains: Sequence[str] = ()) -> AsyncMcpClient:
        return await AsyncMcpClient.start(self.cli + A.mcp_args(audit_dir, allow_domains, self.server), self.environment(), self.cwd)
