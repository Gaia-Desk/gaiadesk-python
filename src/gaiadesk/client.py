"""The synchronous client: each method runs one gaiadesk-cli command (with
``--json`` where the CLI has it) and returns the CLI's own JSON as a dict.
Given ``api_key``, the same methods go to GaiaDesk's hosted API instead
(``_api``): same results and errors, and a UsageError for what it does not serve.
``transport="local"`` / ``"lan"`` send them to a desk's own API (``_local``)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

from . import _args as A
from . import _native as N
from ._api import not_over_api
from ._core import (
    FEATURE_EXEC_CWD,
    FEATURE_SHELL_CWD,
    VERSION_JSON_ARGS,
    Base,
    Completed,
    Plan,
    api_step,
    cached_version_info,
    failure,
    features_of,
    missing_feature,
    not_found,
    parse_json,
    store_version_info,
    version_info_from,
)
from .errors import UsageError
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
        Identity,
        JobInfo,
        JobWaitResult,
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

    The API transport (GaiaDesk's hosted HTTPS API; none of the options above apply):

    * ``api_key``: a GaiaDesk API key (``ak_…``) or a signed-in person's session
      token. Selects the API transport.
    * ``desk_token``: a scoped agent token (``gdagt_…``) sent as
      ``X-GaiaDesk-Desk-Token``. From an API key, desk operations need one.
    * ``base_url``: default ``https://api.gaiadesk.net/v1``.
    * ``wake``: if a desk is asleep, ring it and wait up to this many seconds (0-120).
    * ``e2e``: end-to-end encryption of desk operations, so the server relays only
      ciphertext (needs ``pip install "gaiadesk[e2e]"``). ``"auto"`` (default): seal
      whenever the desk publishes a key, else send in the clear with a warning (or
      raise, for a desk that requires it); ``"require"``: never send in the clear
      (``EndToEndError``); ``"off"``: never seal.
    * ``e2e_keys``: ``{desk_id: e2e_pub}`` to pin desks' keys; a different key from
      the server is an ``EndToEndError`` and nothing is sent.

    The same operations, results and errors, served by a desk itself (``transport=``):

    * ``transport="local"``: code on the desk talks to its own GaiaDesk over the Unix
      socket ``$GAIADESK_API_DIR/api.sock`` (else ``~/.gaiadesk/api.sock``) or, on
      Windows, the named pipe ``\\\\.\\pipe\\gaiadesk-api-<user>`` (``$GAIADESK_API_PIPE``).
      ``socket_path``: another socket path or pipe name. ``desk_token``: an agent
      token, sent as ``X-GaiaDesk-Desk-Token``; without one, the desk's local admin
      token (``token``, else the ``api-token`` file beside the socket) as Bearer.
    * ``transport="lan"``: a desk's LAN gateway. ``base_url`` (``https://<desk>:7443/v1``),
      ``fingerprint`` (its certificate's SHA-256, as the desk's Settings shows it; pinned)
      and ``desk_token`` (required: the gateway takes agent tokens only).
    * ``transport="api"`` / ``"direct"``: the defaults with / without ``api_key``.
    """

    def _run(self, plan: Plan) -> Any:
        if self._api is not None:
            return api_step(plan)(self._api)
        n = self._nat()
        if n is not None and plan.native is not None:
            return n.run_sync(plan.native)
        self._require(plan.requires)
        return plan.finish(self._complete(plan.args, plan.input, plan.env))

    def _require(self, requires: Sequence[Tuple[str, str]]) -> None:
        if requires:
            info = self.cli_version_info()
            err = missing_feature(features_of(info), requires, info)
            if err is not None:
                raise err

    def _complete(self, args: Sequence[str], input: Optional[bytes], env: Optional[Dict[str, str]] = None) -> Completed:
        cmd = self.cli + list(args)
        try:
            p = subprocess.run(cmd, input=input if input is not None else b"", capture_output=True, env=self.run_environment(env), cwd=self.cwd)
        except OSError as e:
            raise not_found(cmd[0], args) from e
        return Completed(p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))

    def _stream(self, args: Sequence[str], input: Optional[bytes] = None, keep_open: bool = False,
                env: Optional[Dict[str, str]] = None) -> CliStream:
        return CliStream(self.cli + list(args), self.run_environment(env), self.cwd, input, keep_open)

    def raw(self, args: Sequence[str], input: Union[None, str, bytes] = None) -> Completed:
        """Run any gaiadesk-cli command; exit code and output untouched. The escape hatch."""
        self._cli_only("raw()")
        return self._complete(list(args), input.encode("utf-8") if isinstance(input, str) else input)

    def version(self) -> str:
        """``gaiadesk-cli --version``."""
        return self._run(self._p_version())

    def cli_version_info(self) -> "Optional[VersionInfo]":
        """``gaiadesk-cli --version --json``: ``{name, version, features,
        mcp_protocol_versions}``, or None from a CLI too old to answer it (no features).
        Asked once per CLI (path, size and mtime) for the whole process. Always the
        CLI, whichever backend runs the operations (not on the API transport)."""
        self._cli_only("cli_version_info()")
        key = self._features_key()
        hit, info = cached_version_info(key)
        if not hit:
            info = version_info_from(self._complete(VERSION_JSON_ARGS, None))
            store_version_info(key, info)
        return info  # type: ignore[return-value]

    def cli_features(self) -> FrozenSet[str]:
        """What this gaiadesk-cli can do (``exec_cwd``, ``run_cwd``, ``shell_cwd``, ...)."""
        return features_of(self.cli_version_info())

    def whoami(self) -> "Identity":
        """``whoami --json``: who this machine is signed in as, ``{source: app|login|token|none, account}``
        (``source == "none"``: not signed in; not an error)."""
        return self._run(self._p_whoami())

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
        env: Optional[Dict[str, str]] = None,
    ) -> "ExecResult":
        """``exec --json``: run ONE command; its exit code, stdout and stderr.

        ``command`` as a str is one command line for the desk's shell; as a list,
        separate arguments. A non-zero exit is a result unless ``check=True``.
        Raises when the command never ran. ``cwd``: the directory it starts in on
        the desk (``--cwd``; a CLI without the ``exec_cwd`` feature is a UsageError).
        ``env``: environment variables for the command, ``{NAME: value}`` (``--env``;
        never logged by the desk). A program Windows Smart App Control / WDAC
        blocked is ``error.reason == "blocked_by_os_policy"``.
        """
        shape = dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist, verbose=verbose, cwd=cwd, env=env)
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
        env: Optional[Dict[str, str]] = None,
    ) -> CliStream:
        """``exec``, streaming. ``stdin=True`` keeps stdin open for ``write()``/``end()``.

        The stream is built from ``exec --json-stream``'s events: the output as
        text, and ``result`` (route, shell, an error's kind, ...) at the end.
        ``json_stream=False`` runs plain ``exec`` instead (the exact bytes; no ``result``).
        ``env`` as for ``exec``.
        """
        A.exec_args(desk_id, command, stdin=False, json=False, cwd=cwd, env=env,
                    shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist)  # validate first
        data = None if stdin is None or isinstance(stdin, bool) else (stdin.encode("utf-8") if isinstance(stdin, str) else stdin)
        if self._api is not None:
            if stdin is True:
                raise not_over_api("exec_stream(stdin=True) (writing stdin as it runs)", "give stdin= as text, or use the CLI or native transport")
            if json_stream is False:
                raise not_over_api("exec_stream(json_stream=False)", "the API streams events; drop json_stream=")
            api_stream = self._api.exec_stream(desk_id, command, data, dict(shell=shell, timeout=timeout, cwd=cwd, env=env))
            return api_stream  # type: ignore[return-value]
        n = self._nat()
        if n is not None:
            shape = dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist, cwd=cwd, env=env)
            return n.stream_sync("exec", N.exec_(desk_id, command, shape), data, stdin is True)  # type: ignore[return-value]
        if cwd is not None:
            self._require([(FEATURE_EXEC_CWD, "exec_stream(cwd=...)")])
        use_events = json_stream is not False
        a = A.exec_args(desk_id, command, stdin=stdin is not None and stdin is not False, json=False, json_stream=use_events,
                        cwd=cwd, env=env, shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist)
        s = self._stream(a, data, keep_open=stdin is True, env=A.cli_env(env))
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
        ``cwd``: where it starts on the desk (``--cwd``; a CLI without the ``shell_cwd`` feature is a UsageError)."""
        shape = dict(shell=shell, timeout=timeout, connect_timeout=connect_timeout, persist=persist, verbose=verbose, cwd=cwd)
        return self._run(self._p_shell(desk_id, script, check, shape))

    def shell_stream(self, desk_id: str, script: Optional[str] = None, *, shell: Optional[str] = None,
                     timeout: Optional[A.Duration] = None, connect_timeout: Optional[A.Duration] = None,
                     cwd: Optional[str] = None) -> CliStream:
        """``shell`` (no --json), streaming. Without ``script``, stdin stays open: ``write()`` lines, then ``end()``.
        ``cwd`` as for ``shell``."""
        self._cli_only("shell_stream()", "use exec_stream(), or the CLI or native transport")
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

    def upload_bytes(self, data: Union[str, bytes], desk_id: str, remote: str) -> "CpSummary":
        """API transport only: write ``data`` to ``remote`` on the desk (``PUT /desks/{id}/files``, at most 256 MB)."""
        if self._api is None:
            raise UsageError("upload_bytes is for the HTTP transports (api, local, lan); use upload() with a local file", kind="usage")
        return self._api.upload_bytes(data, desk_id, remote)

    def download_bytes(self, desk_id: str, remote: str) -> bytes:
        """API transport only: the bytes of ``remote`` on the desk (``GET /desks/{id}/files``, at most 256 MB)."""
        if self._api is None:
            raise UsageError("download_bytes is for the HTTP transports (api, local, lan); use download() to a local file", kind="usage")
        return self._api.download_bytes(desk_id, remote)

    # jobs

    def run_job(self, desk_id: str, name: str, command: A.Command, *, priority: Optional[str] = None,
                cpu: Optional[int] = None, mem: Union[None, int, str] = None, keep_awake: Optional[bool] = None,
                cwd: Optional[str] = None, shell: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> "JobInfo":
        """``run --detach --json``: a named background job that outlives this connection.
        ``cwd``: the directory it starts in on the desk (``--cwd``; a CLI without the ``run_cwd`` feature is a UsageError).
        ``shell``: ``sh``, ``bash``, ``zsh``, ``cmd``, ``pwsh`` or ``powershell`` runs the command (``--shell``; default
        ``sh -c`` / ``cmd /c``). ``env``: environment variables for the job, ``{NAME: value}`` (``--env``)."""
        return self._run(self._p_run_job(desk_id, name, command, dict(priority=priority, cpu=cpu, mem=mem, keep_awake=keep_awake, cwd=cwd,
                                                                      shell=shell, env=env)))

    def wait_job(self, desk_id: str, name: str, *, timeout: Optional[A.Duration] = None) -> "JobWaitResult":
        """``wait <job> --json``: block until the job ends; ``{job, timed_out}``.
        ``timeout`` (seconds or ``"10m"``): give up then, ``timed_out`` True and the job still
        running. A job that ended with a non-zero exit code is a result (``job["exit_code"]``), not an error."""
        return self._run(self._p_wait_job(desk_id, name, timeout))

    def jobs(self, desk_id: str) -> "List[JobInfo]":
        """``ps --json``: the jobs (the list in ``{"jobs": [...]}``)."""
        return self._run(self._p_jobs(desk_id))

    def kill_job(self, desk_id: str, name: str) -> "JobInfo":
        """``kill --json``: stop a job and everything it started."""
        return self._run(self._p_kill_job(desk_id, name))

    def job_logs(self, desk_id: str, name: str, *, tail: Optional[int] = None) -> str:
        """``logs <job> --json``: its output so far, stdout and stderr together."""
        return self._run(self._p_job_logs(desk_id, name, tail))

    def follow_job_logs(self, desk_id: str, name: str, *, tail: Optional[int] = None) -> CliStream:
        """``logs -f --json <job>``: follow until the job ends; ``kill()`` stops following (not the job).
        ``result`` (``end``, ``interrupted`` or ``error``) at the end."""
        a = A.logs_args(desk_id, name, tail, follow=True)
        if self._api is not None:
            return self._api.follow_job_logs(desk_id, name, tail)  # type: ignore[return-value]
        n = self._nat()
        if n is not None:
            return n.stream_sync("job_follow", N.logs(desk_id, name, tail), None, False)  # type: ignore[return-value]
        return JsonExecStream(self._stream(a))  # type: ignore[return-value]

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
        """``token list --json`` (owner only): the tokens (the list in ``{"tokens": [...]}``)."""
        return self._run(self._p_list_tokens(desk_id))

    def revoke_token(self, desk_id: str, name: Optional[str] = None, *, all_for_desk: bool = False, account: bool = False) -> Dict[str, Any]:
        """``token revoke --json``: ``{revoked, stopped_sessions}``; with ``account=True`` ``{desk, ok, message}``."""
        return self._run(self._p_revoke_token(desk_id, name, all_for_desk, account))

    def audit(self, desk_id: str, *, token: Optional[str] = None, limit: Optional[int] = None, account: bool = False) -> "List[AuditEvent]":
        """``audit --json``: what agent tokens did on the desk, newest first (the list in ``{"events": [...]}``)."""
        return self._run(self._p_audit(desk_id, token, limit, account))

    # mesh / connections

    def mesh_status(self) -> "MeshStatus":
        """``mesh status --json``."""
        return self._run(self._p_mesh_status())

    def mesh_ip(self, desk_id: str) -> str:
        """``mesh ip <desk> --json``: the desk's Mesh address."""
        return self._run(self._p_mesh_ip(desk_id))

    def disconnect(self, desk_id: Optional[str] = None) -> "Disconnected":
        """``disconnect --json``: close the held connection to one desk, or all of them; ``{"closed": [...]}``."""
        return self._run(self._p_disconnect(desk_id))

    # forward

    def forward(self, desk_id: str, specs: Union[Dict[str, Any], Sequence[Dict[str, Any]]]) -> Forward:
        """``forward --json``. Each spec: ``remote_port``, optional ``remote_host`` and ``local_port``.
        Returns once every forward is listening. Use as a context manager to stop it."""
        self._cli_only("forward()")
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
        The confirmation line ("agent session open on desk N: screenshot WxH"), made
        from ``agent-connect --json``."""
        self._cli_only("agent_connect()")
        plan = self._p_agent_connect(desk_id)
        n = self._nat()
        if n is not None:
            return n.agent_connect_sync(A.check_desk(desk_id))
        return self._run(plan)

    def mcp(self, *, audit_dir: Optional[str] = None, allow_domains: Sequence[str] = ()) -> McpClient:
        """Start ``gaiadesk-cli mcp`` (stdio): the way to the screen tools from code."""
        self._cli_only("mcp()")
        return McpClient(self.cli + A.mcp_args(audit_dir, allow_domains, self.server), self.environment(), self.cwd)
