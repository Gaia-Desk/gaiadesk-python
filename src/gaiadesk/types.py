"""The JSON gaiadesk-cli prints with ``--json``, field for field (TypedDicts).

Field names are the CLI's own, so these read the same as
``gaiadesk-cli <command> --help`` and https://gaiadesk.net/docs/cli-for-agents.
Results are plain dicts; these types are for editors and type checkers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from typing import Literal, TypedDict

Shell = Literal["default", "none", "sh", "cmd", "pwsh"]


class ExecResult(TypedDict):
    """``exec --json`` / ``shell --json``: {exit, remote_code, stdout, stderr, duration_ms, desk, route, mode, shell, timed_out, error, notes, truncated}."""

    exit: int
    remote_code: Optional[int]
    stdout: str
    stderr: str
    duration_ms: int
    desk: str
    route: Optional[str]
    mode: Optional[str]
    shell: Optional[str]
    timed_out: bool
    error: Optional[str]
    notes: List[str]
    truncated: bool


class ReachSuccess(TypedDict, total=False):
    at: int
    route: str
    connect_ms: int
    rtt_ms: int


class ReachFailure(TypedDict):
    at: int
    kind: str
    message: str


class DeviceRow(TypedDict, total=False):
    desk_id: str
    name: Optional[str]
    online: Optional[bool]
    os: Optional[str]
    app_version: Optional[str]
    owner: Optional[str]
    last_seen: Optional[int]
    sources: List[str]
    signal_idle_secs: int
    anytime: bool
    last_ok: Optional[ReachSuccess]
    last_failure: Optional[ReachFailure]
    reachable: Optional[bool]
    probe: Dict[str, Any]


class DevicesResult(TypedDict):
    devices: List[DeviceRow]
    sources: List[str]
    notes: List[str]


class CpFailure(TypedDict):
    path: str
    message: str


class CpSummary(TypedDict):
    direction: str
    desk: str
    destination: str
    files: int
    dirs: int
    bytes: int
    resumed_bytes: int
    failed: List[CpFailure]
    seconds: float


class JobInfo(TypedDict, total=False):
    name: str
    command: str
    state: str
    pid: int
    exit_code: int
    started_at_ms: int
    ended_at_ms: int
    log_bytes: int
    by: str
    limits: Dict[str, Any]
    enforcement: List[str]


class DeskStats(TypedDict):
    desk: str
    hostname: str
    os: str
    os_version: str
    cpu_percent: float
    cpus: int
    load: Optional[List[float]]
    mem_total_mb: int
    mem_free_mb: int
    disks: List[Dict[str, Any]]
    uptime_secs: int
    jobs_running: int


class MeasureResult(TypedDict):
    desk: str
    sent: int
    rtt_ms: Optional[Dict[str, float]]
    clock_offset_ms: Optional[float]
    clock_uncertainty_ms: Optional[float]


class TokenInfo(TypedDict, total=False):
    label: str
    id: str
    scopes: List[str]
    issued_at_ms: int
    expires_at_ms: int
    revoked: bool
    last_used_ms: int
    cwd: str
    low_priv: bool


class TokenCreateResult(TypedDict, total=False):
    tokens: List[Dict[str, Any]]
    file: str


class AuditEvent(TypedDict, total=False):
    at_ms: int
    desk: str
    token: str
    token_id: str
    action: str
    detail: str
    bytes: int
    cwd: str
    exit_code: int
    duration_ms: int


class MeshStatus(TypedDict):
    self: Optional[Dict[str, Any]]
    peers: List[Dict[str, Any]]


class ForwardListening(TypedDict):
    event: str
    local_port: int
    desk: str
    remote_host: str
    remote_port: int
