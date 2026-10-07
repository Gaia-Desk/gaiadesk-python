"""The asyncio client: the same methods as ``GaiaDesk``, awaitable."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

from . import _args as A
from . import _native as N
from ._core import (
    FEATURE_EXEC_CWD,
    FEATURE_EXEC_JSON_STREAM,
    FEATURE_LOGS_JSON,
    FEATURE_SHELL_CWD,
    VERSION_JSON_ARGS,
    Base,
    Completed,
    Plan,
    cached_version_info,
    failure,
    features_of,
    missing_feature,
    not_found,
    parse_json,
    store_version_info,
    upgraded,
    version_info_from,
)
from .mcp import AsyncMcpClient
from .stream import AsyncCliStream, AsyncJsonExecStream, Exit

if TYPE_CHECKING:  # gaiadesk.types needs typing_extensions before Python 3.11; nothing here needs it at runtime
    from .types import (
        AuditEvent,
        CpSummary,
        DeviceRow,
        DevicesResult,
        ExecResult,
        ForwardListening,
        JobInfo,
        MeasureResult,
        MeshStatus,
        Disconnected,
        StatsReport,
        TokenCreateResult,
        TokenInfo,
        VersionInfo,
    )


class AsyncForward:
    """A running ``gaiadesk-cli forward``. ``listening``: one entry per forward."""

    def __init__(self, stream: AsyncCliStream, listening: "List[ForwardListening]") -> None:
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
        n = self._nat()
        if n is not None and plan.native is not None:
            return await n.run_async(plan.native)
        await self._require(plan.requires)
        if plan.upgrade is not None:
            plan = upgraded(plan, await self.cli_features())
        return plan.finish(await self._complete(plan.args, plan.input))

    async def _require(self, requires: Sequence[Tuple[str, str]]) -> None:
        if requires:
            info = await self.cli_version_info()
            err = missing_feature(features_of(info), requires, info)
            if err is not None:
                raise err

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

    async def cli_version_info(self) -> "Optional[VersionInfo]":
        """As ``GaiaDesk.cli_version_info`` (shares its per-process cache)."""
        key = self._features_key()
        hit, info = cached_version_info(key)
        if not hit:
            info = version_info_from(await self._complete(VERSION_JSON_ARGS, None))
            store_version_info(key, info)
        return info  # type: ignore[return-value]

    async def cli_features(self) -> FrozenSet[str]:
        return features_of(await self.cli_version_info())

    async def devices(self, *, probe: bool = False, desk_id: Optional[str] = None) -> "DevicesResult":
        return await self._run(self._p_devices(probe, desk_id))

    async def probe(self, desk_id: str) -> "DeviceRow":
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
                   persist: Optional[A.Duration] = None, verbose: bool = False, cwd: Optional[str] = None) -> "ExecResult":
        shape = dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist, verbose=verbose, cwd=cwd)
        return await self._run(self._p_exec(desk_id, command, stdin, check, shape))

    async def exec_stream(self, desk_id: str, command: A.Command, *, stdin: Union[None, str, bytes, bool] = None,
                          shell: Optional[str] = None, timeout: Optional[A.Duration] = None,
                          connect_timeout: Optional[A.Duration] = None, persist: Optional[A.Duration] = None,
                          cwd: Optional[str] = None, json_stream: Optional[bool] = None) -> AsyncCliStream:
        """As ``GaiaDesk.exec_stream``: ``exec --json-stream`` events when the CLI has them."""
        A.exec_args(desk_id, command, stdin=False, json=False, cwd=cwd,
                    shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist)  # validate first
        data = None if stdin is None or isinstance(stdin, bool) else (stdin.encode("utf-8") if isinstance(stdin, str) else stdin)
        n = self._nat()
        if n is not None:
            shape = dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist, cwd=cwd)
            return await n.stream_async("exec", N.exec_(desk_id, command, shape), data, stdin is True)  # type: ignore[return-value]
        need: List[Tuple[str, str]] = []
        if cwd is not None:
            need.append((FEATURE_EXEC_CWD, "exec_stream(cwd=...)"))
        if json_stream:
            need.append((FEATURE_EXEC_JSON_STREAM, "exec_stream(json_stream=True)"))
        await self._require(need)
        use_events = json_stream is not False and FEATURE_EXEC_JSON_STREAM in await self.cli_features()
        a = A.exec_args(desk_id, command, stdin=stdin is not None and stdin is not False, json=False, json_stream=use_events,
                        cwd=cwd, shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist)
        s = await self._stream(a, data, keep_open=stdin is True)
        return AsyncJsonExecStream(s) if use_events else s  # type: ignore[return-value]

    async def shell(self, desk_id: str, script: str, *, check: bool = False, shell: Optional[str] = None,
                    timeout: Optional[A.Duration] = None, connect_timeout: Optional[A.Duration] = None,
                    persist: Optional[A.Duration] = None, verbose: bool = False, cwd: Optional[str] = None) -> "ExecResult":
        shape = dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist, verbose=verbose, cwd=cwd)
        return await self._run(self._p_shell(desk_id, script, check, shape))

    async def shell_stream(self, desk_id: str, script: Optional[str] = None, *, shell: Optional[str] = None,
                           timeout: Optional[A.Duration] = None, connect_timeout: Optional[A.Duration] = None,
                           cwd: Optional[str] = None) -> AsyncCliStream:
        a = A.shell_args(desk_id, json=False, shell=shell, timeout=timeout, connect_timeout=connect_timeout, cwd=cwd)
        n = self._nat()
        if n is not None:
            na = N.shell_stream(desk_id, dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, cwd=cwd))
            return await n.stream_async("shell", na, script.encode("utf-8") if script is not None else None, script is None)  # type: ignore[return-value]
        if cwd is not None:
            await self._require([(FEATURE_SHELL_CWD, "shell_stream(cwd=...)")])
        return await self._stream(a, script.encode("utf-8") if script is not None else None, keep_open=script is None)

    async def upload(self, local: str, desk_id: str, remote: str, *, recursive: bool = False) -> "CpSummary":
        return await self._run(self._p_cp("upload", desk_id, local, remote, recursive))

    async def download(self, desk_id: str, remote: str, local: str, *, recursive: bool = False) -> "CpSummary":
        return await self._run(self._p_cp("download", desk_id, local, remote, recursive))

    async def run_job(self, desk_id: str, name: str, command: A.Command, *, priority: Optional[str] = None,
                      cpu: Optional[int] = None, mem: Union[None, int, str] = None, keep_awake: Optional[bool] = None,
                      cwd: Optional[str] = None) -> "JobInfo":
        return await self._run(self._p_run_job(desk_id, name, command, dict(priority=priority, cpu=cpu, mem=mem, keep_awake=keep_awake, cwd=cwd)))

    async def jobs(self, desk_id: str) -> "List[JobInfo]":
        return await self._run(self._p_jobs(desk_id))

    async def kill_job(self, desk_id: str, name: str) -> "JobInfo":
        return await self._run(self._p_kill_job(desk_id, name))

    async def job_logs(self, desk_id: str, name: str, *, tail: Optional[int] = None) -> str:
        return await self._run(self._p_job_logs(desk_id, name, tail))

    async def follow_job_logs(self, desk_id: str, name: str, *, tail: Optional[int] = None) -> AsyncCliStream:
        a = A.logs_args(desk_id, name, tail, follow=True)
        n = self._nat()
        if n is not None:
            return await n.stream_async("job_follow", N.logs(desk_id, name, tail), None, False)  # type: ignore[return-value]
        if FEATURE_LOGS_JSON in await self.cli_features():
            return AsyncJsonExecStream(await self._stream(A.logs_args(desk_id, name, tail, follow=True, json=True)))  # type: ignore[return-value]
        return await self._stream(a)

    async def stats(self, desk_id: str) -> "StatsReport":
        return await self._run(self._p_stats(desk_id))

    async def measure(self, desk_id: str, *, count: Optional[int] = None) -> "MeasureResult":
        return await self._run(self._p_measure(desk_id, count))

    async def create_token(self, desks: Union[str, Sequence[str]], *, name: Optional[str] = None, expires: Optional[str] = None,
                           scopes: Optional[Sequence[str]] = None, cwd: Optional[str] = None, low_priv: bool = False,
                           out: Optional[str] = None) -> "TokenCreateResult":
        return await self._run(self._p_create_token(desks, dict(name=name, expires=expires, scopes=scopes, cwd=cwd, low_priv=low_priv, out=out)))

    async def list_tokens(self, desk_id: str) -> "List[TokenInfo]":
        return await self._run(self._p_list_tokens(desk_id))

    async def revoke_token(self, desk_id: str, name: Optional[str] = None, *, all_for_desk: bool = False,
                           account: bool = False) -> Dict[str, Any]:
        return await self._run(self._p_revoke_token(desk_id, name, all_for_desk, account))

    async def audit(self, desk_id: str, *, token: Optional[str] = None, limit: Optional[int] = None,
                    account: bool = False) -> "List[AuditEvent]":
        return await self._run(self._p_audit(desk_id, token, limit, account))

    async def mesh_status(self) -> "MeshStatus":
        return await self._run(self._p_mesh_status())

    async def mesh_ip(self, desk_id: str) -> str:
        return await self._run(self._p_mesh_ip(desk_id))

    async def disconnect(self, desk_id: Optional[str] = None) -> "Disconnected":
        return await self._run(self._p_disconnect(desk_id))

    async def forward(self, desk_id: str, specs: Union[Dict[str, Any], Sequence[Dict[str, Any]]]) -> AsyncForward:
        lst = [specs] if isinstance(specs, dict) else list(specs)
        a = A.forward_args(desk_id, lst)
        n = self._nat()
        if n is not None:
            return await n.forward_async(N.forward(desk_id, lst))  # type: ignore[return-value]
        s = await self._stream(a)
        listening: "List[ForwardListening]" = []
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
        plan = self._p_agent_connect(desk_id)
        n = self._nat()
        if n is not None:
            return await n.agent_connect_async(A.check_desk(desk_id))
        return await self._run(plan)

    async def mcp(self, *, audit_dir: Optional[str] = None, allow_domains: Sequence[str] = ()) -> AsyncMcpClient:
        return await AsyncMcpClient.start(self.cli + A.mcp_args(audit_dir, allow_domains, self.server), self.environment(), self.cwd)
