"""What the sync and async clients share: options, the environment, locating
gaiadesk-cli, and turning a finished run into a result or a typed error.

Each operation is a ``Plan``: the argv, the stdin bytes, and a ``finish``
function from the completed run to the result, plus the same operation for
the native backend (``_native.NativeReq``). ``GaiaDesk`` runs plans with
``subprocess`` (or the native library); ``AsyncGaiaDesk`` with ``asyncio``.
"""

from __future__ import annotations

import json as _json
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union

from . import _args as A
from . import _native as N
from .errors import (
    CliNotFoundError,
    GaiaDeskError,
    ProtocolError,
    UsageError,
    error_envelope,
    error_from_run,
    exec_outcome,
)

FEATURE_EXEC_CWD = "exec_cwd"
FEATURE_RUN_CWD = "run_cwd"
FEATURE_SHELL_CWD = "shell_cwd"

DOWNLOAD_URL = "https://gaiadesk.net/download"


@dataclass
class Completed:
    """A finished gaiadesk-cli run."""

    code: Optional[int]
    stdout: str
    stderr: str


class Plan(NamedTuple):
    args: List[str]
    input: Optional[bytes]
    finish: Callable[[Completed], Any]
    native: Optional[N.NativeReq] = None
    requires: Tuple[Tuple[str, str], ...] = ()
    """``(feature, what)`` pairs the CLI must list in ``--version --json`` for this run (CLI backend only)."""


def _b(data: Union[None, str, bytes]) -> Optional[bytes]:
    if data is None:
        return None
    return data.encode("utf-8") if isinstance(data, str) else bytes(data)


# ───────────────────────────── locating gaiadesk-cli ─────────────────────────────


def standard_locations(platform: str, env: Mapping[str, str], home: Optional[str] = None) -> List[str]:
    """Typical install locations (GaiaDesk docs: "Where gaiadesk-cli is")."""
    if platform == "darwin":
        l = ["/Applications/GaiaDesk.app/Contents/MacOS/gaiadesk-cli"]
        if home:
            l.append(home + "/Applications/GaiaDesk.app/Contents/MacOS/gaiadesk-cli")
        l.append("/usr/local/bin/gaiadesk-cli")
        return l
    if platform == "win32":
        roots: List[str] = []
        for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            r = env.get(k)
            if r and r not in roots:
                roots.append(r)
        l = [r + "\\GaiaDesk\\gaiadesk-cli.exe" for r in roots]
        la = env.get("LOCALAPPDATA")
        if la:
            l += [la + "\\GaiaDesk\\gaiadesk-cli.exe", la + "\\Programs\\GaiaDesk\\gaiadesk-cli.exe"]
        return l or ["C:\\Program Files\\GaiaDesk\\gaiadesk-cli.exe"]
    l = ["/usr/bin/gaiadesk-cli", "/usr/local/bin/gaiadesk-cli"]
    if home:
        l.append(home + "/.local/bin/gaiadesk-cli")
    return l


def path_candidates(platform: str, env: Mapping[str, str]) -> List[str]:
    raw = (env.get("Path") or env.get("PATH")) if platform == "win32" else env.get("PATH")
    if not raw:
        return []
    sep, slash = (";", "\\") if platform == "win32" else (":", "/")
    name = "gaiadesk-cli.exe" if platform == "win32" else "gaiadesk-cli"
    out = []
    for d in raw.split(sep):
        d = d.strip().strip('"')
        if d:
            out.append(d + name if d.endswith(slash) else d + slash + name)
    return out


def locate_cli(env: Mapping[str, str], platform: str = sys.platform, exists: Callable[[str], bool] = os.path.isfile) -> str:
    """$GAIADESK_CLI, then PATH, then the standard locations; else the bare name."""
    if env.get("GAIADESK_CLI"):
        return env["GAIADESK_CLI"]
    home = env.get("USERPROFILE") if platform == "win32" else env.get("HOME")
    for c in path_candidates(platform, env) + standard_locations(platform, env, home):
        if exists(c):
            return c
    return "gaiadesk-cli.exe" if platform == "win32" else "gaiadesk-cli"


def not_found(program: str, args: Sequence[str]) -> GaiaDeskError:
    from .errors import CliNotFoundError

    return CliNotFoundError(
        "could not run %r. Install GaiaDesk from %s, or pass the full path of gaiadesk-cli as cli= (or set GAIADESK_CLI)."
        % (program, DOWNLOAD_URL),
        kind="not_found",
        argv=args,
    )


# ───────────────────────────── feature detection ─────────────────────────────

VERSION_JSON_ARGS = ["--version", "--json"]

_features_lock = threading.Lock()
_features_cache: Dict[Tuple[Any, ...], Optional[Dict[str, Any]]] = {}


def features_key(cli: Sequence[str]) -> Tuple[Any, ...]:
    """The cache key for one CLI: its command vector, and the program file's
    size and mtime (an upgraded CLI is asked again)."""
    stamp: Tuple[Any, ...] = ()
    for part in cli:
        try:
            st = os.stat(part)
        except (OSError, ValueError):
            continue
        stamp += (part, st.st_size, st.st_mtime_ns)
    return (tuple(cli),) + stamp


def cached_version_info(key: Tuple[Any, ...]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    with _features_lock:
        if key in _features_cache:
            return True, _features_cache[key]
        return False, None


def store_version_info(key: Tuple[Any, ...], info: Optional[Dict[str, Any]]) -> None:
    with _features_lock:
        _features_cache[key] = info


def clear_feature_cache() -> None:
    """Forget what every CLI said it can do (they are asked again on next use)."""
    with _features_lock:
        _features_cache.clear()


def version_info_from(done: Completed) -> Optional[Dict[str, Any]]:
    """``--version --json``'s VersionInfo, or None from a CLI too old to answer
    it (it prints its version as text, or fails on the flag): no features."""
    if done.code != 0:
        return None
    v = parse_json(done.stdout)
    if not isinstance(v, dict) or not isinstance(v.get("features"), list):
        return None
    return v


def features_of(info: Optional[Mapping[str, Any]]) -> FrozenSet[str]:
    if not info:
        return frozenset()
    return frozenset(f for f in info.get("features", []) if isinstance(f, str))


def missing_feature(have: FrozenSet[str], requires: Sequence[Tuple[str, str]], info: Optional[Mapping[str, Any]]) -> Optional[GaiaDeskError]:
    """A UsageError for the first required feature this CLI lacks (never run without it)."""
    for feature, what in requires:
        if feature not in have:
            ver = info.get("version") if info else None
            return UsageError(
                "%s: this gaiadesk-cli (%s) does not list %r in `--version --json`. "
                "Update gaiadesk-cli from %s, or leave %s out." % (what, ver or "too old to say", feature, DOWNLOAD_URL, what),
                kind="usage",
                argv=VERSION_JSON_ARGS,
            )
    return None


# ───────────────────────────── parsing ─────────────────────────────


def parse_json(stdout: str) -> Any:
    t = stdout.strip()
    if not t:
        return None
    try:
        return _json.loads(t)
    except ValueError:
        try:
            return _json.loads(t.splitlines()[-1])
        except ValueError:
            return None


def failure(done: Completed, args: Sequence[str], parsed: Any) -> GaiaDeskError:
    """The error for a failed run (see ``errors.error_from_run``: the one place that reads the CLI's error envelopes)."""
    return error_from_run(done.code, done.stderr, args, parsed)


def op_finish(args: Sequence[str], ok: Sequence[int] = (0,)) -> Callable[[Completed], Any]:
    def finish(done: Completed) -> Any:
        parsed = parse_json(done.stdout)
        if done.code in ok and parsed is not None and error_envelope(parsed) is None:
            return parsed
        if done.code == 0 and parsed is None:
            raise ProtocolError("gaiadesk-cli printed no JSON", exit_code=0, stderr=done.stderr, argv=args, kind="protocol")
        raise failure(done, args, parsed)

    return finish


def exec_finish(args: Sequence[str], check: bool) -> Callable[[Completed], Any]:
    def finish(done: Completed) -> Any:
        r = parse_json(done.stdout)
        if not isinstance(r, dict) or not isinstance(r.get("exit"), int):
            if done.code != 0:
                raise failure(done, args, r)
            raise ProtocolError("gaiadesk-cli printed no exec JSON", exit_code=done.code, stderr=done.stderr, argv=args, kind="protocol")
        return exec_outcome(r, check, done.code, done.stderr, args)

    return finish


def unwrap_list(key: str, args: Sequence[str]) -> Callable[[Any], Any]:
    """``{"<key>": [...]}``: the list."""

    def unwrap(v: Any) -> Any:
        if isinstance(v, dict) and isinstance(v.get(key), list):
            return v[key]
        raise ProtocolError("gaiadesk-cli printed no {%r: [...]}" % key, kind="protocol", argv=args, json=v)

    return unwrap


def list_finish(args: Sequence[str], key: str) -> Callable[[Completed], Any]:
    inner, unwrap = op_finish(args), unwrap_list(key, args)
    return lambda done: unwrap(inner(done))


def text_finish(args: Sequence[str], strip: bool = False) -> Callable[[Completed], Any]:
    def finish(done: Completed) -> Any:
        if done.code != 0:
            raise failure(done, args, None)
        return done.stdout.strip() if strip else done.stdout

    return finish


def field_finish(args: Sequence[str], key: str) -> Callable[[Completed], Any]:
    """A ``--json`` object's text field (``logs --json`` -> ``output``, ``mesh ip --json`` -> ``mesh_ip``)."""
    inner = op_finish(args)

    def finish(done: Completed) -> Any:
        v = inner(done)
        if isinstance(v, dict) and isinstance(v.get(key), str):
            return v[key]
        raise ProtocolError("gaiadesk-cli printed no %r" % key, kind="protocol", argv=args, json=v)

    return finish


def agent_check_finish(args: Sequence[str]) -> Callable[[Completed], Any]:
    """``agent-connect --json``'s ``{desk_id, ok, screenshot}`` as the CLI's text line."""
    inner = op_finish(args)

    def finish(done: Completed) -> Any:
        v = inner(done)
        shot = v.get("screenshot") if isinstance(v, dict) else None
        if not isinstance(shot, dict):
            raise ProtocolError("gaiadesk-cli printed no agent-connect result", kind="protocol", argv=args, json=v)
        return "agent session open on desk %s: screenshot %sx%s" % (v.get("desk_id"), shot.get("width"), shot.get("height"))

    return finish


def wait_finish(args: Sequence[str]) -> Callable[[Completed], Any]:
    """``wait --json``: the job, and the exit code is the JOB's (124: --timeout ran
    out with the job still running). ``{job, timed_out}``, as the native library."""

    def finish(done: Completed) -> Any:
        parsed = parse_json(done.stdout)
        if isinstance(parsed, dict) and error_envelope(parsed) is None and isinstance(parsed.get("name"), str):
            return {"job": parsed, "timed_out": done.code == 124 and parsed.get("state") == "running"}
        if done.code == 0 and parsed is None:
            raise ProtocolError("gaiadesk-cli printed no job", exit_code=0, stderr=done.stderr, argv=args, kind="protocol")
        raise failure(done, args, parsed)

    return finish


def none_finish(args: Sequence[str]) -> Callable[[Completed], Any]:
    def finish(done: Completed) -> None:
        if done.code != 0:
            raise failure(done, args, None)

    return finish


# ───────────────────────────── options + plans ─────────────────────────────


class Base:
    """Options and plans. See ``GaiaDesk`` for the parameters."""

    def __init__(
        self,
        *,
        cli: Union[None, str, Sequence[str]] = None,
        token_file: Optional[str] = None,
        code: Optional[str] = None,
        account_token: Optional[str] = None,
        agent_token: Optional[str] = None,
        server: Optional[str] = None,
        persist: Optional[A.Duration] = None,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        backend: Optional[str] = None,
        native: Any = None,
    ) -> None:
        self._backend_opt = backend
        self._native_mod = native
        self._native: Any = False  # False: not decided yet; None: the CLI
        self._cli_opt = cli
        self._cli: Optional[List[str]] = None
        self.token_file = token_file
        self.code = code
        self.account_token = account_token
        self.agent_token = agent_token
        self.server = server
        self.persist = persist
        self.base_env = env
        self.cwd = cwd

    @property
    def cli(self) -> List[str]:
        """The command vector used to run gaiadesk-cli."""
        if self._cli is None:
            c = self._cli_opt
            if c is None:
                self._cli = [locate_cli(self.base_env if self.base_env is not None else os.environ)]
            elif isinstance(c, str):
                self._cli = [c]
            else:
                self._cli = list(c)
            if not self._cli:
                raise UsageError("cli must not be empty", kind="usage")
        return self._cli

    def environment(self) -> Dict[str, str]:
        """The environment gaiadesk-cli runs with: the base env plus the configured credentials."""
        env = dict(self.base_env if self.base_env is not None else os.environ)
        if self.token_file is not None:
            env["GAIADESK_TOKEN_FILE"] = self.token_file
        if self.code is not None:
            env["GAIADESK_CODE"] = self.code
            # An explicit code must not lose to an inherited token file.
            if self.token_file is None:
                env.pop("GAIADESK_TOKEN_FILE", None)
        if self.account_token is not None:
            env["GAIADESK_TOKEN"] = self.account_token
        if self.agent_token is not None:
            env["GAIADESK_AGENT_TOKEN"] = self.agent_token
        if self.server is not None:
            env["GAIADESK_SERVER"] = self.server
        if self.persist is not None:
            env["GAIADESK_PERSIST"] = A.duration(self.persist, "persist")
        return env

    # The backend.

    def _nat(self) -> Optional[N.NativeBackend]:
        """The native backend when this client uses it (decided once, on first use)."""
        if self._native is not False:
            return self._native
        base = self.base_env if self.base_env is not None else os.environ
        want = self._backend_opt or base.get("GAIADESK_SDK_BACKEND") or "auto"
        if want not in ("auto", "native", "cli"):
            raise UsageError("backend is auto, native or cli (not %r)" % (want,), kind="usage")
        mod, why = None, "not wanted"
        if want == "native" or (want == "auto" and self._cli_opt is None):
            mod, why = (self._native_mod, "") if self._native_mod is not None else N.load_native()
        if mod is None and want == "native":
            raise CliNotFoundError("backend='native', but gaiadesk_native is not usable here: %s "
                                   "(pip install gaiadesk[native])" % why, kind="not_found")
        if mod is None:
            self._native = None
            return None
        try:
            self._native = N.NativeBackend(mod.NativeClient(N.native_options(self.environment(), self.cwd)))
        except Exception as e:
            raise N.from_native(e, "client") from e
        return self._native

    @property
    def backend(self) -> str:
        """Which backend runs the operations: ``native`` (gaiadesk_native) or ``cli`` (gaiadesk-cli)."""
        return "native" if self._nat() is not None else "cli"

    # What the CLI can do (``--version --json``), asked once per CLI.

    def _features_key(self) -> Tuple[Any, ...]:
        return features_key(self.cli)

    # The plans. Public methods in client.py / aio.py run these.

    def _p_version(self) -> Plan:
        return Plan(["--version"], None, text_finish(["--version"], strip=True), N.NativeReq("version", {}, None, N.version_finish))

    def _p_devices(self, probe: bool, desk_id: Optional[str]) -> Plan:
        a = A.devices_args(probe, desk_id)
        return Plan(a, None, op_finish(a, (0, 1)), N.NativeReq("devices", N.devices(probe, desk_id)))

    def _p_exec(self, desk_id: str, command: A.Command, stdin: Union[None, str, bytes], check: bool, shape: Dict[str, Any]) -> Plan:
        a = A.exec_args(desk_id, command, stdin=stdin is not None, json=True, **shape)
        req = ((FEATURE_EXEC_CWD, "exec(cwd=...)"),) if shape.get("cwd") is not None else ()
        return Plan(a, _b(stdin), exec_finish(a, check),
                    N.NativeReq("exec", N.exec_(desk_id, command, shape), _b(stdin), N.exec_finish("exec", check)), req)

    def _p_shell(self, desk_id: str, script: str, check: bool, shape: Dict[str, Any]) -> Plan:
        a = A.shell_args(desk_id, json=True, **shape)
        req = ((FEATURE_SHELL_CWD, "shell(cwd=...)"),) if shape.get("cwd") is not None else ()
        return Plan(a, _b(script), exec_finish(a, check),
                    N.NativeReq("shell", N.shell(desk_id, script, shape), None, N.exec_finish("shell", check)), req)

    def _p_cp(self, direction: str, desk_id: str, local: str, remote: str, recursive: bool) -> Plan:
        a = A.cp_args(direction, desk_id, local, remote, recursive)
        nargs = {"desk_id": A.check_desk(desk_id), "local": local, "remote": remote, "recursive": recursive}
        return Plan(a, None, op_finish(a), N.NativeReq(direction, nargs, None, N.cp_finish(direction)))

    def _p_op(self, a: List[str], ok: Sequence[int] = (0,), native: Optional[N.NativeReq] = None) -> Plan:
        return Plan(a, None, op_finish(a, ok), native)

    def _p_text(self, a: List[str], strip: bool = False, native: Optional[N.NativeReq] = None) -> Plan:
        return Plan(a, None, text_finish(a, strip), native)

    def _p_none(self, a: List[str], native: Optional[N.NativeReq] = None) -> Plan:
        return Plan(a, None, none_finish(a), native)

    def _p_mesh_ip(self, desk_id: str) -> Plan:
        a = A.mesh_ip_args(desk_id)
        return Plan(a, None, field_finish(a, "mesh_ip"), N.NativeReq("mesh_ip", N.desk(desk_id), None, N.field_finish("mesh_ip")))

    def _p_mesh_status(self) -> Plan:
        return self._p_op(["mesh", "status", "--json"], native=N.NativeReq("mesh_status", {}))

    def _p_run_job(self, desk_id: str, name: str, command: A.Command, limits: Dict[str, Any]) -> Plan:
        a = A.run_args(desk_id, name, command, **limits)
        req = ((FEATURE_RUN_CWD, "run_job(cwd=...)"),) if limits.get("cwd") is not None else ()
        return Plan(a, None, op_finish(a), N.NativeReq("job_run", N.run_job(desk_id, name, command, limits)), req)

    def _p_list(self, a: List[str], key: str, op: str, nargs: Dict[str, Any]) -> Plan:
        """``ps`` / ``token list`` / ``audit``: the list in ``{"<key>": [...]}``."""
        return Plan(a, None, list_finish(a, key), N.NativeReq(op, nargs, None, unwrap_list(key, [op])))

    def _p_wait_job(self, desk_id: str, name: str, timeout: Optional[A.Duration]) -> Plan:
        a = A.wait_args(desk_id, name, timeout)
        return Plan(a, None, wait_finish(a), N.NativeReq("job_wait", N.wait_job(desk_id, name, timeout)))

    def _p_whoami(self) -> Plan:
        a = A.whoami_args()
        return self._p_op(a, (0, 1), native=N.NativeReq("whoami", {}))

    def _p_jobs(self, desk_id: str) -> Plan:
        return self._p_list(A.ps_args(desk_id), "jobs", "job_list", N.desk(desk_id))

    def _p_kill_job(self, desk_id: str, name: str) -> Plan:
        return self._p_op(A.kill_args(desk_id, name), native=N.NativeReq("job_kill", N.job(desk_id, name)))

    def _p_job_logs(self, desk_id: str, name: str, tail: Optional[int]) -> Plan:
        a = A.logs_args(desk_id, name, tail)
        return Plan(a, None, field_finish(a, "output"), N.NativeReq("job_logs", N.logs(desk_id, name, tail), None, N.field_finish("output")))

    def _p_stats(self, desk_id: str) -> Plan:
        return self._p_op(A.stats_args(desk_id), native=N.NativeReq("stats", N.desk(desk_id)))

    def _p_measure(self, desk_id: str, count: Optional[int]) -> Plan:
        return self._p_op(A.measure_args(desk_id, count), (0, 1), native=N.NativeReq("measure", N.measure(desk_id, count)))

    def _p_create_token(self, desks: Union[str, Sequence[str]], spec: Dict[str, Any]) -> Plan:
        return self._p_op(A.token_create_args(desks, **spec), native=N.NativeReq("token_mint", N.token_create(desks, spec)))

    def _p_list_tokens(self, desk_id: str) -> Plan:
        return self._p_list(A.token_list_args(desk_id), "tokens", "token_list", N.desk(desk_id))

    def _p_revoke_token(self, desk_id: str, name: Optional[str], all_for_desk: bool, account: bool) -> Plan:
        return self._p_op(A.token_revoke_args(desk_id, name, all_for_desk, account),
                          native=N.NativeReq("token_revoke", N.token_revoke(desk_id, name, all_for_desk, account)))

    def _p_audit(self, desk_id: str, token: Optional[str], limit: Optional[int], account: bool) -> Plan:
        return self._p_list(A.audit_args(desk_id, token, limit, account), "events", "audit", N.audit(desk_id, token, limit, account))

    def _p_disconnect(self, desk_id: Optional[str]) -> Plan:
        a = A.disconnect_args(desk_id)
        return self._p_op(a, native=N.NativeReq("disconnect", {} if desk_id is None else N.desk(desk_id)))

    def _p_agent_connect(self, desk_id: str) -> Plan:
        a = A.agent_connect_args(desk_id, self.server)
        return Plan(a, None, agent_check_finish(a))
