"""Arguments for the native backend's operations: snake_case, as the native
library takes them. Callers validate first with the CLI builder for the same
call (``_args``), so bad input is the same UsageError on both backends. Pure."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

from . import _args as A
from .errors import UsageError


def _shape(shape: Mapping[str, Any]) -> Dict[str, Any]:
    s: Dict[str, Any] = {}
    if shape.get("shell") is not None:
        s["shell"] = A.wire_shell(shape["shell"])
    for k, flag in (("timeout", "--timeout"), ("connect_timeout", "--connect-timeout"), ("persist", "--persist")):
        if shape.get(k) is not None:
            s[k] = A.duration(shape[k], flag)
    if shape.get("verbose"):
        s["verbose"] = True
    if shape.get("cwd") is not None:
        s["cwd"] = A.check_cwd(shape["cwd"])
    if shape.get("env") is not None:
        s["env"] = A.check_env(shape["env"])
    return s


def _cmd(command: Any) -> Any:
    return command if isinstance(command, str) else list(command)


def desk(desk_id: str) -> Dict[str, Any]:
    return {"desk_id": A.check_desk(desk_id)}


def devices(probe: bool, desk_id: Optional[str]) -> Dict[str, Any]:
    a: Dict[str, Any] = {"probe": bool(probe)}
    if desk_id is not None:
        a.update(desk(desk_id))
    return a


def exec_(desk_id: str, command: Any, shape: Mapping[str, Any]) -> Dict[str, Any]:
    return dict(desk(desk_id), command=_cmd(command), **_shape(shape))


def shell(desk_id: str, script: str, shape: Mapping[str, Any]) -> Dict[str, Any]:
    return dict(desk(desk_id), script=script, **_shape(shape))


def shell_stream(desk_id: str, shape: Mapping[str, Any]) -> Dict[str, Any]:
    return dict(desk(desk_id), **_shape(shape))


def mem_mb(mem: Any) -> int:
    """``mem``: megabytes, or ``"512M"`` / ``"4G"``, as ``run --mem`` takes it."""
    if isinstance(mem, int) and not isinstance(mem, bool):
        return mem
    m = re.match(r"^\s*(\d+)\s*([MGmg])?[Bb]?\s*$", str(mem))
    if not m:
        raise UsageError("mem: not a size: %r" % (mem,), kind="usage")
    return int(m.group(1)) * (1024 if (m.group(2) or "").upper() == "G" else 1)


def run_job(desk_id: str, name: str, command: Any, limits: Mapping[str, Any]) -> Dict[str, Any]:
    lim: Dict[str, Any] = {}
    if limits.get("priority") is not None:
        lim["priority"] = limits["priority"]
    if limits.get("cpu") is not None:
        lim["cpu_percent"] = limits["cpu"]
    if limits.get("mem") is not None:
        lim["mem_mb"] = mem_mb(limits["mem"])
    if limits.get("keep_awake") is not None:
        lim["keep_awake"] = limits["keep_awake"]
    a = dict(desk(desk_id), name=name, command=_cmd(command), limits=lim)
    if limits.get("cwd") is not None:
        a["cwd"] = A.check_cwd(limits["cwd"])
    if limits.get("shell") is not None:
        a["shell"] = A.wire_shell(limits["shell"])
    if limits.get("env") is not None:
        a["env"] = A.check_env(limits["env"])
    return a


def job(desk_id: str, name: str) -> Dict[str, Any]:
    return dict(desk(desk_id), name=name)


def wait_job(desk_id: str, name: str, timeout: Any) -> Dict[str, Any]:
    a = job(desk_id, name)
    if timeout is not None:
        a["timeout"] = A.duration(timeout, "--timeout")
    return a


def logs(desk_id: str, name: str, tail: Optional[int]) -> Dict[str, Any]:
    a = job(desk_id, name)
    if tail is not None:
        a["tail"] = tail
    return a


def measure(desk_id: str, count: Optional[int]) -> Dict[str, Any]:
    a = desk(desk_id)
    if count is not None:
        a["count"] = count
    return a


def token_create(desks: Any, spec: Mapping[str, Any]) -> Dict[str, Any]:
    a: Dict[str, Any] = {"desks": [A.check_desk(d) for d in ([desks] if isinstance(desks, str) else list(desks))]}
    for k in ("name", "expires", "cwd", "out"):
        if spec.get(k) is not None:
            a[k] = spec[k]
    if spec.get("scopes") is not None:
        a["scopes"] = list(spec["scopes"])
    if spec.get("low_priv"):
        a["low_priv"] = True
    return a


def token_revoke(desk_id: str, name: Optional[str], all_for_desk: bool, account: bool) -> Dict[str, Any]:
    a = dict(desk(desk_id), account=bool(account))
    if all_for_desk:
        a["all"] = True
    else:
        a["which"] = name
    return a


def audit(desk_id: str, token: Optional[str], limit: Optional[int], account: bool) -> Dict[str, Any]:
    a = dict(desk(desk_id), account=bool(account))
    if token is not None:
        a["token"] = token
    if limit is not None:
        a["limit"] = limit
    return a


def forward(desk_id: str, specs: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = []
    for s in specs:
        f: Dict[str, Any] = {"remote_port": s["remote_port"]}
        if s.get("remote_host") is not None:
            f["remote_host"] = s["remote_host"]
        if s.get("local_port"):
            f["local_port"] = s["local_port"]
        out.append(f)
    return dict(desk(desk_id), specs=out)
