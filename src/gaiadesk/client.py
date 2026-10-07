"""The synchronous client: each method runs one gaiadesk-cli command (with
``--json`` where the CLI has it) and returns the CLI's own JSON as a dict."""

from __future__ import annotations

import subprocess
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
from .mcp import McpClient
from .stream import CliStream, Exit, JsonExecStream

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


class Forward:
    """A running ``gaiadesk-cli forward``. ``listening``: one entry per forward."""

    def __init__(self, stream: CliStream, listening: "List[ForwardListening]") -> None:
        self._stream = stream
        self.listening = listening

    def close(self) -> Exit:
        """Stop forwarding and wait for gaiadesk-cli to exit."""
        self._stream.kill()
        return self._stream.wait()

    def wait(self) -> Exit:
        """Block until forwarding ends (exit 254: the desk refused; 255: connection lost)."""
        return self._stream.wait()

    def __enter__(self) -> "Forward":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class GaiaDesk(Base):
    """Drive GaiaDesk desks through ``gaiadesk-cli``.

    Parameters (all optional, keyword-only):

    * ``cli``: gaiadesk-cli's path, or a command list. Default: $GAIADESK_CLI,
      then PATH, then the standard install locations.
    * ``token_file``: a scoped agent token file. Sets GAIADESK_TOKEN_FILE.
    * ``code``: the desk's code or unattended password. Sets GAIADESK_CODE
      (never argv). Token administration needs the unattended password.
    * ``account_token``: a GaiaDesk account session token. Sets GAIADESK_TOKEN.
      Default: the CLI's own ``gaiadesk-cli login``.
    * ``agent_token``: an agent token for screen tools (mcp, agent_connect).
      Sets GAIADESK_AGENT_TOKEN.
    * ``server``: signaling URL (``wss://.../ws``). Sets GAIADESK_SERVER; passed
      as ``--server`` to mcp and agent-connect.
    * ``persist``: how long desk connections are held (seconds or ``"10m"``).
    * ``env``: the base environment (default: ``os.environ``).
    * ``cwd``: gaiadesk-cli's working directory.
    * ``backend``: ``auto`` (default: the native library when ``gaiadesk_native``
      is installed and no ``cli`` was given, else gaiadesk-cli), ``native`` or
      ``cli``. Default from $GAIADESK_SDK_BACKEND. ``raw()``/``mcp()`` always use the CLI.
    * ``native``: a module to use instead of ``import gaiadesk_native``.
    """

    def _run(self, plan: Plan) -> Any:
        n = self._nat()
        if n is not None and plan.native is not None:
            return n.run_sync(plan.native)
        self._require(plan.requires)
        if plan.upgrade is not None:
            plan = upgraded(plan, self.cli_features())
        return plan.finish(self._complete(plan.args, plan.input))

    def _require(self, requires: Sequence[Tuple[str, str]]) -> None:
        if requires:
            info = self.cli_version_info()
            err = missing_feature(features_of(info), requires, info)
            if err is not None:
                raise err

    def _complete(self, args: Sequence[str], input: Optional[bytes]) -> Completed:
        cmd = self.cli + list(args)
        try:
            p = subprocess.run(cmd, input=input if input is not None else b"", capture_output=True, env=self.environment(), cwd=self.cwd)
        except OSError as e:
            raise not_found(cmd[0], args) from e
        return Completed(p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))

    def _stream(self, args: Sequence[str], input: Optional[bytes] = None, keep_open: bool = False) -> CliStream:
        return CliStream(self.cli + list(args), self.environment(), self.cwd, input, keep_open)

    def raw(self, args: Sequence[str], input: Union[None, str, bytes] = None) -> Completed:
        """Run any gaiadesk-cli command; exit code and output untouched. The escape hatch."""
        return self._complete(list(args), input.encode("utf-8") if isinstance(input, str) else input)

    def version(self) -> str:
        """``gaiadesk-cli --version``."""
        return self._run(self._p_version())

    def cli_version_info(self) -> "Optional[VersionInfo]":
        """``gaiadesk-cli --version --json``: ``{name, version, features, json_shapes,
        mcp_protocol_versions}``, or None from a CLI before 0.10.324 (no features).
        Asked once per CLI (path, size and mtime) for the whole process. Always the
        CLI, whichever backend runs the operations."""
        key = self._features_key()
        hit, info = cached_version_info(key)
        if not hit:
            info = version_info_from(self._complete(VERSION_JSON_ARGS, None))
            store_version_info(key, info)
        return info  # type: ignore[return-value]

    def cli_features(self) -> FrozenSet[str]:
        """What this gaiadesk-cli can do (``exec_json_stream``, ``exec_cwd``, ``run_cwd``, ...); empty for an older CLI."""
        return features_of(self.cli_version_info())

    # devices

    def devices(self, *, probe: bool = False, desk_id: Optional[str] = None) -> "DevicesResult":
        """``devices --json [--probe] [-d id]``. With probe, an unreachable desk has ``reachable: False`` (CLI exit 1, not an error here)."""
        return self._run(self._p_devices(probe, desk_id))

    def probe(self, desk_id: str) -> "DeviceRow":
        """``devices --probe -d <id>``: is this desk reachable right now?"""
        rows = self.devices(probe=True, desk_id=desk_id)["devices"]
        for r in rows:
            if r.get("desk_id") == desk_id:
                return r
        if rows:
            return rows[0]
        from .errors import ProtocolError

        raise ProtocolError("devices --probe listed no row for %s" % desk_id, kind="protocol")

    # exec / shell

    def exec(
        self,
        desk_id: str,
        command: A.Command,
        *,
        stdin: Union[None, str, bytes] = None,
        check: bool = False,
        shell: Optional[str] = None,
        timeout: Optional[A.Duration] = None,
        connect_timeout: Optional[A.Duration] = None,
        persist: Optional[A.Duration] = None,
        verbose: bool = False,
        cwd: Optional[str] = None,
    ) -> "ExecResult":
        """``exec --json``: run ONE command; its exit code, stdout and stderr.

        ``command`` as a str is one command line for the desk's shell; as a list,
        separate arguments. A non-zero exit is a result unless ``check=True``.
        Raises when the command never ran. ``cwd``: the directory it starts in on
        the desk (``--cwd``; gaiadesk-cli 0.10.324+, else a UsageError).
        """
        shape = dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist, verbose=verbose, cwd=cwd)
        return self._run(self._p_exec(desk_id, command, stdin, check, shape))

    def exec_stream(
        self,
        desk_id: str,
        command: A.Command,
        *,
        stdin: Union[None, str, bytes, bool] = None,
        shell: Optional[str] = None,
        timeout: Optional[A.Duration] = None,
        connect_timeout: Optional[A.Duration] = None,
        persist: Optional[A.Duration] = None,
        cwd: Optional[str] = None,
        json_stream: Optional[bool] = None,
    ) -> CliStream:
        """``exec``, streaming. ``stdin=True`` keeps stdin open for ``write()``/``end()``.

        With a gaiadesk-cli that has ``exec --json-stream`` (0.10.324+) the stream
        is built from its events: the output as text, and ``result`` (route, shell,
        an error's kind, ...) at the end. ``json_stream=False`` forces plain
        ``exec`` (the exact bytes; no ``result``), ``True`` requires the events.
        Older CLIs: plain ``exec``.
        """
        A.exec_args(desk_id, command, stdin=False, json=False, cwd=cwd,
                    shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist)  # validate first
        data = None if stdin is None or isinstance(stdin, bool) else (stdin.encode("utf-8") if isinstance(stdin, str) else stdin)
        n = self._nat()
        if n is not None:
            shape = dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist, cwd=cwd)
            return n.stream_sync("exec", N.exec_(desk_id, command, shape), data, stdin is True)  # type: ignore[return-value]
        need: List[Tuple[str, str]] = []
        if cwd is not None:
            need.append((FEATURE_EXEC_CWD, "exec_stream(cwd=...)"))
        if json_stream:
            need.append((FEATURE_EXEC_JSON_STREAM, "exec_stream(json_stream=True)"))
        self._require(need)
        use_events = json_stream is not False and FEATURE_EXEC_JSON_STREAM in self.cli_features()
        a = A.exec_args(desk_id, command, stdin=stdin is not None and stdin is not False, json=False, json_stream=use_events,
                        cwd=cwd, shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist)
        s = self._stream(a, data, keep_open=stdin is True)
        return JsonExecStream(s) if use_events else s  # type: ignore[return-value]

    def shell(
        self,
        desk_id: str,
        script: str,
        *,
        check: bool = False,
        shell: Optional[str] = None,
        timeout: Optional[A.Duration] = None,
        connect_timeout: Optional[A.Duration] = None,
        persist: Optional[A.Duration] = None,
        verbose: bool = False,
        cwd: Optional[str] = None,
    ) -> "ExecResult":
        """``shell --json`` with ``script`` on stdin: run in the desk's shell over plain pipes; the script's exit code.
        ``cwd``: where it starts on the desk (``--cwd``; gaiadesk-cli 0.10.324+, feature ``shell_cwd``, else a UsageError)."""
        shape = dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist, verbose=verbose, cwd=cwd)
        return self._run(self._p_shell(desk_id, script, check, shape))

    def shell_stream(self, desk_id: str, script: Optional[str] = None, *, shell: Optional[str] = None,
                     timeout: Optional[A.Duration] = None, connect_timeout: Optional[A.Duration] = None,
                     cwd: Optional[str] = None) -> CliStream:
        """``shell`` (no --json), streaming. Without ``script``, stdin stays open: ``write()`` lines, then ``end()``.
        ``cwd`` as for ``shell``."""
        a = A.shell_args(desk_id, json=False, shell=shell, timeout=timeout, connect_timeout=connect_timeout, cwd=cwd)
        n = self._nat()
        if n is not None:
            na = N.shell_stream(desk_id, dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, cwd=cwd))
            return n.stream_sync("shell", na, script.encode("utf-8") if script is not None else None, script is None)  # type: ignore[return-value]
        if cwd is not None:
            self._require([(FEATURE_SHELL_CWD, "shell_stream(cwd=...)")])
        return self._stream(a, script.encode("utf-8") if script is not None else None, keep_open=script is None)

    # cp

    def upload(self, local: str, desk_id: str, remote: str, *, recursive: bool = False) -> "CpSummary":
        """``cp --json <local> <desk>:<remote>``. Raises OperationFailedError (summary in ``.json``) if a file failed."""
        return self._run(self._p_cp("upload", desk_id, local, remote, recursive))

    def download(self, desk_id: str, remote: str, local: str, *, recursive: bool = False) -> "CpSummary":
        """``cp --json <desk>:<remote> <local>``."""
        return self._run(self._p_cp("download", desk_id, local, remote, recursive))

    # jobs

    def run_job(self, desk_id: str, name: str, command: A.Command, *, priority: Optional[str] = None,
                cpu: Optional[int] = None, mem: Union[None, int, str] = None, keep_awake: Optional[bool] = None,
                cwd: Optional[str] = None) -> "JobInfo":
        """``run --detach --json``: a named background job that outlives this connection.
        ``cwd``: the directory it starts in on the desk (``--cwd``; gaiadesk-cli 0.10.324+, else a UsageError)."""
        return self._run(self._p_run_job(desk_id, name, command, dict(priority=priority, cpu=cpu, mem=mem, keep_awake=keep_awake, cwd=cwd)))

    def jobs(self, desk_id: str) -> "List[JobInfo]":
        """``ps --json``: the jobs (the CLI's ``{"jobs": [...]}``, or an older CLI's bare array)."""
        return self._run(self._p_jobs(desk_id))

    def kill_job(self, desk_id: str, name: str) -> "JobInfo":
        """``kill --json``: stop a job and everything it started."""
        return self._run(self._p_kill_job(desk_id, name))

    def job_logs(self, desk_id: str, name: str, *, tail: Optional[int] = None) -> str:
        """``logs <job> --json`` (an older CLI's plain ``logs``): its output so far, stdout and stderr together."""
        return self._run(self._p_job_logs(desk_id, name, tail))

    def follow_job_logs(self, desk_id: str, name: str, *, tail: Optional[int] = None) -> CliStream:
        """``logs -f <job>``: follow until the job ends; ``kill()`` stops following (not the job).
        On gaiadesk-cli 0.10.324+ (``logs_json``) it runs ``logs -f --json``: the same chunks,
        and ``result`` (``end``, ``interrupted`` or ``error``) at the end."""
        a = A.logs_args(desk_id, name, tail, follow=True)
        n = self._nat()
        if n is not None:
            return n.stream_sync("job_follow", N.logs(desk_id, name, tail), None, False)  # type: ignore[return-value]
        if FEATURE_LOGS_JSON in self.cli_features():
            return JsonExecStream(self._stream(A.logs_args(desk_id, name, tail, follow=True, json=True)))  # type: ignore[return-value]
        return self._stream(a)

    # stats / measure

    def stats(self, desk_id: str) -> "StatsReport":
        """``stats --json``."""
        return self._run(self._p_stats(desk_id))

    def measure(self, desk_id: str, *, count: Optional[int] = None) -> "MeasureResult":
        """``measure --json``. ``rtt_ms`` is None if no ping came back (CLI exit 1)."""
        return self._run(self._p_measure(desk_id, count))

    # tokens / audit

    def create_token(self, desks: Union[str, Sequence[str]], *, name: Optional[str] = None, expires: Optional[str] = None,
                     scopes: Optional[Sequence[str]] = None, cwd: Optional[str] = None, low_priv: bool = False,
                     out: Optional[str] = None) -> "TokenCreateResult":
        """``token create --json`` (owner: needs ``code`` = the unattended password). Without ``out`` each entry has the ``secret``."""
        return self._run(self._p_create_token(desks, dict(name=name, expires=expires, scopes=scopes, cwd=cwd, low_priv=low_priv, out=out)))

    def list_tokens(self, desk_id: str) -> "List[TokenInfo]":
        """``token list --json`` (owner only): the tokens (``{"tokens": [...]}``, or an older CLI's bare array)."""
        return self._run(self._p_list_tokens(desk_id))

    def revoke_token(self, desk_id: str, name: Optional[str] = None, *, all_for_desk: bool = False, account: bool = False) -> Dict[str, Any]:
        """``token revoke --json``: ``{revoked, stopped_sessions}``; with ``account=True`` ``{desk, ok, message}``."""
        return self._run(self._p_revoke_token(desk_id, name, all_for_desk, account))

    def audit(self, desk_id: str, *, token: Optional[str] = None, limit: Optional[int] = None, account: bool = False) -> "List[AuditEvent]":
        """``audit --json``: what agent tokens did on the desk, newest first (``{"events": [...]}``, or a bare array)."""
        return self._run(self._p_audit(desk_id, token, limit, account))

    # mesh / connections

    def mesh_status(self) -> "MeshStatus":
        """``mesh status --json``."""
        return self._run(self._p_mesh_status())

    def mesh_ip(self, desk_id: str) -> str:
        """``mesh ip <desk> --json`` (an older CLI's text): the desk's Mesh address."""
        return self._run(self._p_mesh_ip(desk_id))

    def disconnect(self, desk_id: Optional[str] = None) -> "Disconnected":
        """``disconnect --json``: close the held connection to one desk, or all of them; ``{"closed": [...]}``."""
        return self._run(self._p_disconnect(desk_id))

    # forward

    def forward(self, desk_id: str, specs: Union[Dict[str, Any], Sequence[Dict[str, Any]]]) -> Forward:
        """``forward --json``. Each spec: ``remote_port``, optional ``remote_host`` and ``local_port``.
        Returns once every forward is listening. Use as a context manager to stop it."""
        lst = [specs] if isinstance(specs, dict) else list(specs)
        a = A.forward_args(desk_id, lst)
        n = self._nat()
        if n is not None:
            return n.forward_sync(N.forward(desk_id, lst))  # type: ignore[return-value]
        s = self._stream(a)
        listening: "List[ForwardListening]" = []
        buf = ""
        stderr = ""
        for c in s:
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
                return Forward(s, listening)
        e = s.wait()
        raise failure(Completed(e.exit_code, "", stderr), a, None)

    # Agent Access (screen)

    def agent_connect(self, desk_id: str) -> str:
        """``agent-connect``: prove an agent token opens a screen session (needs ``agent_token``).
        The confirmation line ("agent session open on desk N: screenshot WxH"); from
        ``agent-connect --json`` on gaiadesk-cli 0.10.324+."""
        plan = self._p_agent_connect(desk_id)
        n = self._nat()
        if n is not None:
            return n.agent_connect_sync(A.check_desk(desk_id))
        return self._run(plan)

    def mcp(self, *, audit_dir: Optional[str] = None, allow_domains: Sequence[str] = ()) -> McpClient:
        """Start ``gaiadesk-cli mcp`` (stdio): the way to the screen tools from code."""
        return McpClient(self.cli + A.mcp_args(audit_dir, allow_domains, self.server), self.environment(), self.cwd)
