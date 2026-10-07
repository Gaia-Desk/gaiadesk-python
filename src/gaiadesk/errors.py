"""Typed errors, mapped from what gaiadesk-cli reports.

Exit codes (``gaiadesk-cli --help``):

* exec/shell: 0-255 the remote command's own; 124 ``--timeout`` ran out;
  130 interrupted; 253 (shell) remote error / connection lost; 254 the desk
  refused; 255 gaiadesk-cli's own error.
* desk operations (cp, run, ps, logs, kill, stats, token, audit, ...):
  0 done; 1 ran and did not succeed; 254 refused; 255 own error.

``exec --json`` / ``shell --json`` failures before the command ran carry
``"error": {"kind", "message"}`` with kind one of: usage, offline,
unknown_desk, not_online, refused, network, not_signed_in, timeout,
connection_lost, local.
"""

from __future__ import annotations

from typing import Any, Dict, NamedTuple, Optional, Sequence


class GaiaDeskError(Exception):
    """Base class. ``exit_code``, ``kind``, ``stderr``, ``argv`` and ``json`` describe the failure."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: Optional[int] = None,
        kind: str = "cli_error",
        stderr: str = "",
        argv: Sequence[str] = (),
        json: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.kind = kind
        self.stderr = stderr
        self.argv = list(argv)
        self.json = json


class CliNotFoundError(GaiaDeskError):
    """gaiadesk-cli could not be started (not installed, wrong path)."""


class UsageError(GaiaDeskError):
    """Bad arguments (kind ``usage``), caught by the SDK or by gaiadesk-cli."""


class RefusedError(GaiaDeskError):
    """The desk said no: wrong code, a token without the scope, expired/revoked, permission off (exit 254)."""


class UnreachableError(GaiaDeskError):
    """The desk could not be reached: offline, unknown_desk, not_online, network, not_signed_in, timeout."""


class ConnectionLostError(GaiaDeskError):
    """The connection went away mid-command (kind ``connection_lost``, shell exit 253)."""


class OperationFailedError(GaiaDeskError):
    """A desk operation ran and did not succeed (exit 1): a file failed to copy, no such job, ..."""


class ProtocolError(GaiaDeskError):
    """gaiadesk-cli printed something that is not the JSON it documents."""


class CommandError(GaiaDeskError):
    """``exec``/``shell`` with ``check=True``: the remote command exited non-zero (or timed out)."""

    def __init__(self, message: str, result: Any, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.result = result


class McpError(GaiaDeskError):
    """A JSON-RPC error from ``gaiadesk-cli mcp`` (e.g. -32602, -41001 no credential)."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message, kind="protocol")
        self.code = code
        self.data = data


_UNREACHABLE = {"offline", "unknown_desk", "not_online", "network", "not_signed_in", "timeout"}


def error_for_kind(kind: str, message: str, **details: Any) -> GaiaDeskError:
    details["kind"] = kind
    if kind == "usage":
        return UsageError(message, **details)
    if kind == "refused":
        return RefusedError(message, **details)
    if kind == "connection_lost":
        return ConnectionLostError(message, **details)
    if kind in _UNREACHABLE:
        return UnreachableError(message, **details)
    return GaiaDeskError(message, **details)


def error_for_exit(code: Optional[int], message: str, **details: Any) -> GaiaDeskError:
    if code == 254:
        details["kind"] = "refused"
        return RefusedError(message, **details)
    if code == 253:
        details["kind"] = "connection_lost"
        return ConnectionLostError(message, **details)
    if code == 1:
        details["kind"] = "failed"
        return OperationFailedError(message, **details)
    if code == 130:
        details["kind"] = "interrupted"
        return GaiaDeskError(message, **details)
    details.setdefault("kind", "cli_error")
    return GaiaDeskError(message, **details)


class ErrorEnvelope(NamedTuple):
    """What a gaiadesk-cli JSON output says went wrong."""

    kind: Optional[str]  # a machine-readable kind, when the CLI gave one (today only exec/shell do)
    message: str


def error_envelope(parsed: Any) -> Optional[ErrorEnvelope]:
    """THE place that knows how gaiadesk-cli spells an error in its JSON.

    Every error path in the SDK goes through here, so a change to the CLI's
    error envelope is a change to this function only. Shapes recognized
    today::

        {"error": {"kind": "...", "message": "..."}}   exec/shell, before the command ran
        {"error": "..."}                                most desk operations
        {"refused": "..."}                              cp

    Returns None when the JSON is not an error (including exec's own
    ``"error": null`` on success).
    """
    if not isinstance(parsed, dict):
        return None
    e = parsed.get("error")
    if isinstance(e, dict) and isinstance(e.get("kind"), str):
        m = e.get("message")
        return ErrorEnvelope(e["kind"], m if isinstance(m, str) else "")
    if isinstance(e, str) and e:
        return ErrorEnvelope(None, e)
    r = parsed.get("refused")
    if isinstance(r, str) and r:
        return ErrorEnvelope(None, r)
    return None


def error_from_run(code: Optional[int], stderr: str, argv: Sequence[str], parsed: Any) -> GaiaDeskError:
    """The typed error for a failed run: the JSON's error envelope (with its
    kind when it has one), else the exit code with the best message available
    (envelope, a ``{"desk", "ok": False, "message"}`` reply, or stderr)."""
    details: Dict[str, Any] = dict(exit_code=code, stderr=stderr, argv=argv, json=parsed)
    env = error_envelope(parsed)
    if env is not None and env.kind is not None:
        return error_for_kind(env.kind, env.message, **details)
    msg = env.message if env is not None else ""
    if not msg and isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
        msg = parsed["message"]
    msg = msg or last_stderr_line(stderr) or "gaiadesk-cli exited with %s" % code
    return error_for_exit(code, msg, **details)


def last_stderr_line(stderr: str) -> str:
    """The last thing gaiadesk-cli said on stderr, without its ``gaiadesk-cli: `` prefix."""
    lines = [l.strip() for l in stderr.splitlines()]
    lines = [l for l in lines if l and not l.startswith("(see `gaiadesk-cli")]
    last = lines[-1] if lines else ""
    return last[len("gaiadesk-cli:"):].strip() if last.startswith("gaiadesk-cli:") else last
