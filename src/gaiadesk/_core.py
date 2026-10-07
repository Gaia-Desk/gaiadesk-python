"""What the sync and async clients share: options, the environment, locating
gaiadesk-cli, and turning a finished run into a result or a typed error.

Each operation is a ``Plan``: the argv, the stdin bytes, and a ``finish``
function from the completed run to the result. ``GaiaDesk`` runs plans with
``subprocess``; ``AsyncGaiaDesk`` with ``asyncio``.
"""

from __future__ import annotations

import json as _json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Union

from . import _args as A
from .errors import (
    CommandError,
    GaiaDeskError,
    ProtocolError,
    RefusedError,
    UsageError,
    error_envelope,
    error_from_run,
    last_stderr_line,
)

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
        details: Dict[str, Any] = dict(exit_code=done.code, stderr=done.stderr, argv=args, json=r)
        env = error_envelope(r)
        # A failure before the command ran: an envelope with a kind.
        if env is not None and env.kind is not None:
            raise failure(done, args, r)
        # The desk refused the command itself (e.g. a token without `exec`): exit 254, it never ran.
        if r["exit"] == 254 and r.get("remote_code") in (-1, None):
            details["kind"] = "refused"
            raise RefusedError((env.message if env else "") or last_stderr_line(done.stderr) or "the desk refused the command", **details)
        if check and r["exit"] != 0:
            why = "timed out" if r.get("timed_out") else "exited %d" % r["exit"]
            raise CommandError("command on desk %s %s" % (r.get("desk"), why), r, **details)
        return r

    return finish


def text_finish(args: Sequence[str], strip: bool = False) -> Callable[[Completed], Any]:
    def finish(done: Completed) -> Any:
        if done.code != 0:
            raise failure(done, args, None)
        return done.stdout.strip() if strip else done.stdout

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
    ) -> None:
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

    # The plans. Public methods in client.py / aio.py run these.

    def _p_version(self) -> Plan:
        return Plan(["--version"], None, text_finish(["--version"], strip=True))

    def _p_devices(self, probe: bool, desk_id: Optional[str]) -> Plan:
        a = A.devices_args(probe, desk_id)
        return Plan(a, None, op_finish(a, (0, 1)))

    def _p_exec(self, desk_id: str, command: A.Command, stdin: Union[None, str, bytes], check: bool, shape: Dict[str, Any]) -> Plan:
        a = A.exec_args(desk_id, command, stdin=stdin is not None, json=True, **shape)
        return Plan(a, _b(stdin), exec_finish(a, check))

    def _p_shell(self, desk_id: str, script: str, check: bool, shape: Dict[str, Any]) -> Plan:
        a = A.shell_args(desk_id, json=True, **shape)
        return Plan(a, _b(script), exec_finish(a, check))

    def _p_cp(self, direction: str, desk_id: str, local: str, remote: str, recursive: bool) -> Plan:
        a = A.cp_args(direction, desk_id, local, remote, recursive)
        return Plan(a, None, op_finish(a))

    def _p_op(self, a: List[str], ok: Sequence[int] = (0,)) -> Plan:
        return Plan(a, None, op_finish(a, ok))

    def _p_text(self, a: List[str], strip: bool = False) -> Plan:
        return Plan(a, None, text_finish(a, strip))

    def _p_none(self, a: List[str]) -> Plan:
        return Plan(a, None, none_finish(a))

    def _p_mesh_ip(self, desk_id: str) -> Plan:
        return self._p_text(["mesh", "ip", A.check_desk(desk_id)], strip=True)
