"""gaiadesk-cli argument vectors, built from options. Pure (no I/O).

Flags are exactly those in ``gaiadesk-cli <cmd> --help``. Credentials never
go in argv (every user on a machine can read a command line); the client
passes them through the environment instead.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Union

from .errors import UsageError

SHELLS = ("default", "none", "sh", "cmd", "pwsh")
Duration = Union[int, float, str]
Command = Union[str, Sequence[str]]


def _usage(msg: str) -> UsageError:
    return UsageError(msg, kind="usage")


def check_desk(desk_id: str) -> str:
    """A desk id: one token, no whitespace, not a flag."""
    if not isinstance(desk_id, str) or not desk_id.strip():
        raise _usage("a desk id is required")
    d = desk_id.strip()
    if re.search(r"\s", d) or d.startswith("-"):
        raise _usage("not a desk id: %r" % desk_id)
    return d


def check_job_name(name: str) -> str:
    """``ps``, ``logs`` and ``kill`` take the name as a positional, so it must not look like a flag."""
    if not isinstance(name, str) or not re.match(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$", name):
        raise _usage("a job name is letters, digits, . _ - (not starting with -): %r" % (name,))
    return name


def duration(v: Duration, flag: str) -> str:
    """A number is whole seconds (rounded up; the CLI reads a bare integer as seconds); a string is passed as written."""
    if isinstance(v, bool):
        raise _usage("%s must be a number of seconds or a duration string" % flag)
    if isinstance(v, (int, float)):
        if not math.isfinite(v) or v < 0:
            raise _usage("%s must be a number of seconds >= 0" % flag)
        return str(int(math.ceil(v)))
    if not isinstance(v, str) or not re.match(r"^\s*\d+\s*[a-zA-Z]*(\s*\d+\s*[a-zA-Z]+)*\s*$", v):
        raise _usage("%s: not a duration: %r" % (flag, v))
    return v.strip()


def _argv(command: Command, what: str) -> List[str]:
    argv = [command] if isinstance(command, str) else list(command)
    if not argv or (len(argv) == 1 and not argv[0].strip()):
        raise _usage("%s needs a command" % what)
    return argv


def shape_flags(
    shell: Optional[str] = None,
    timeout: Optional[Duration] = None,
    connect_timeout: Optional[Duration] = None,
    persist: Optional[Duration] = None,
    verbose: bool = False,
) -> List[str]:
    a: List[str] = []
    if shell is not None:
        if shell not in SHELLS:
            raise _usage("shell is one of " + ", ".join(SHELLS))
        a += ["--shell", shell]
    if timeout is not None:
        a += ["--timeout", duration(timeout, "--timeout")]
    if connect_timeout is not None:
        d = duration(connect_timeout, "--connect-timeout")
        if d == "0":
            raise _usage("--connect-timeout must be more than 0")
        a += ["--connect-timeout", d]
    if persist is not None:
        a += ["--persist", duration(persist, "--persist")]
    if verbose:
        a.append("--verbose")
    return a


def exec_args(desk_id: str, command: Command, *, stdin: bool, json: bool, **shape: Any) -> List[str]:
    """``exec --desk-id <id> [flags] -- <command>``. A str is ONE command line; a list is separate arguments."""
    argv = _argv(command, "exec")
    a = ["exec", "--desk-id", check_desk(desk_id), "--quiet"]
    if json:
        a.append("--json")
    a.append("--stdin" if stdin else "--no-stdin")
    a += shape_flags(**shape)
    return a + ["--"] + argv


def shell_args(desk_id: str, *, json: bool, **shape: Any) -> List[str]:
    """``shell --desk-id <id> [flags]`` with a script on stdin (plain pipes, like exec)."""
    a = ["shell", "--desk-id", check_desk(desk_id), "--quiet"]
    if json:
        a.append("--json")
    return a + shape_flags(**shape)


def devices_args(probe: bool = False, desk_id: Optional[str] = None) -> List[str]:
    a = ["devices", "--json"]
    if probe:
        a.append("--probe")
    if desk_id is not None:
        a += ["--desk-id", check_desk(desk_id)]
    return a


def local_path(p: str) -> str:
    """A local path gaiadesk-cli would read as ``<desk>:<path>`` (2+ letters/digits before ':') or as a flag gets ``./``."""
    if not isinstance(p, str) or not p:
        raise _usage("a local path is required")
    if re.match(r"^[A-Za-z0-9]{2,}:", p) or p.startswith("-"):
        return "./" + p
    return p


def cp_args(direction: str, desk_id: str, local: str, remote: str, recursive: bool) -> List[str]:
    d = check_desk(desk_id)
    if not isinstance(remote, str):
        raise _usage("a remote path is required")
    a = ["cp"]
    if recursive:
        a.append("--recursive")
    a.append("--json")
    r = "%s:%s" % (d, remote)
    l = local_path(local)
    return a + ([l, r] if direction == "upload" else [r, l])


def run_args(
    desk_id: str,
    name: str,
    command: Command,
    *,
    priority: Optional[str] = None,
    cpu: Optional[int] = None,
    mem: Optional[Union[int, str]] = None,
    keep_awake: Optional[bool] = None,
) -> List[str]:
    """``run --detach --name <job> --desk-id <id> [caps] --json -- <command>``."""
    argv = _argv(command, "run")
    a = ["run", "--detach", "--name", check_job_name(name), "--desk-id", check_desk(desk_id)]
    if priority is not None:
        if priority not in ("low", "normal", "high"):
            raise _usage("priority is low, normal or high")
        a += ["--priority", priority]
    if cpu is not None:
        if isinstance(cpu, bool) or not isinstance(cpu, int) or not 1 <= cpu <= 100:
            raise _usage("cpu is a share of the whole machine, 1 to 100")
        a += ["--cpu", str(cpu)]
    if mem is not None:
        a += ["--mem", str(mem)]
    if keep_awake is True:
        a.append("--keep-awake")
    elif keep_awake is False:
        a.append("--no-keep-awake")
    return a + ["--json", "--"] + argv


def ps_args(desk_id: str) -> List[str]:
    return ["ps", "--desk-id", check_desk(desk_id), "--json"]


def kill_args(desk_id: str, name: str) -> List[str]:
    return ["kill", check_job_name(name), "--desk-id", check_desk(desk_id), "--json"]


def logs_args(desk_id: str, name: str, tail: Optional[int] = None, follow: bool = False) -> List[str]:
    a = ["logs", check_job_name(name), "--desk-id", check_desk(desk_id)]
    if follow:
        a.append("--follow")
    if tail is not None:
        if isinstance(tail, bool) or not isinstance(tail, int) or tail < 0:
            raise _usage("tail is a number of bytes")
        a += ["--tail", str(tail)]
    return a


def stats_args(desk_id: str) -> List[str]:
    return ["stats", "--desk-id", check_desk(desk_id), "--json"]


def measure_args(desk_id: str, count: Optional[int] = None) -> List[str]:
    a = ["measure", "--desk-id", check_desk(desk_id)]
    if count is not None:
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
            raise _usage("count is 1-1000")
        a += ["--count", str(count)]
    return a + ["--json"]


def token_create_args(
    desks: Union[str, Sequence[str]],
    *,
    name: Optional[str] = None,
    expires: Optional[str] = None,
    scopes: Optional[Sequence[str]] = None,
    cwd: Optional[str] = None,
    low_priv: bool = False,
    out: Optional[str] = None,
) -> List[str]:
    ds = [check_desk(d) for d in ([desks] if isinstance(desks, str) else list(desks))]
    if not ds:
        raise _usage("at least one desk is required")
    a = ["token", "create", "--desk", ",".join(ds)]
    if name is not None:
        a += ["--name", name]
    if expires is not None:
        a += ["--expires", expires]
    if scopes is not None:
        if not scopes:
            raise _usage("scopes must not be empty")
        a += ["--scope", ",".join(scopes)]
    if cwd is not None:
        a += ["--cwd", cwd]
    if low_priv:
        a.append("--low-priv")
    if out is not None:
        a += ["--out", out]
    return a + ["--json"]


def token_list_args(desk_id: str) -> List[str]:
    return ["token", "list", "--desk", check_desk(desk_id), "--json"]


def token_revoke_args(desk_id: str, name: Optional[str], all_for_desk: bool, account: bool) -> List[str]:
    a = ["token", "revoke", "--desk", check_desk(desk_id)]
    if all_for_desk and name is not None:
        raise _usage("give a token name or id, or all_for_desk=True, not both")
    if all_for_desk:
        a.append("--all-for-desk")
    elif name and not name.startswith("-"):
        a.append(name)
    else:
        raise _usage("a token name or id is required (or all_for_desk=True)")
    if account:
        a.append("--account")
    return a + ["--json"]


def audit_args(desk_id: str, token: Optional[str] = None, limit: Optional[int] = None, account: bool = False) -> List[str]:
    a = ["audit", "--desk", check_desk(desk_id)]
    if token is not None:
        a += ["--token", token]
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise _usage("limit is a positive number of entries")
        a += ["--limit", str(limit)]
    if account:
        a.append("--account")
    return a + ["--json"]


def _port(v: Any, lo: int, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= 65535:
        raise _usage("%s must be %d-65535" % (what, lo))
    return v


def forward_args(desk_id: str, specs: Sequence[Dict[str, Any]]) -> List[str]:
    """Each spec: ``remote_port`` (required), ``remote_host``, ``local_port`` (0/absent: a free one)."""
    d = check_desk(desk_id)
    if not specs:
        raise _usage("at least one forward is required")
    a = ["forward", "--json"]
    for s in specs:
        rp = _port(s.get("remote_port"), 1, "remote_port")
        lp = _port(s.get("local_port", 0) or 0, 0, "local_port")
        host = s.get("remote_host")
        if host is not None and (not host or re.search(r"[\s:]", host)):
            raise _usage("remote_host is a host name or IPv4 address")
        a += ["%s:%s:%d" % (d, host, rp) if host else "%s:%d" % (d, rp), "localhost:%d" % lp]
    return a


def disconnect_args(desk_id: Optional[str] = None) -> List[str]:
    return ["disconnect", "--all"] if desk_id is None else ["disconnect", "--desk-id", check_desk(desk_id)]


def agent_connect_args(desk_id: str, server: Optional[str] = None) -> List[str]:
    a = ["agent-connect", "--desk-id", check_desk(desk_id)]
    if server:
        a += ["--server", server]
    return a


def mcp_args(audit_dir: Optional[str] = None, allow_domains: Sequence[str] = (), server: Optional[str] = None) -> List[str]:
    a = ["mcp"]
    if server:
        a += ["--server", server]
    for d in allow_domains:
        a += ["--allow-domain", d]
    if audit_dir:
        a += ["--audit-dir", audit_dir]
    return a
