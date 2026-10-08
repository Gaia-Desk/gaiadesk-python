"""Typed errors, mapped from what gaiadesk-cli reports.

Every ``--json`` failure is ONE envelope on stdout::

    {"error": {"kind": "...", "message": "...", "reason": "...", "desk": "..."}}

``kind`` is one of six: usage, refused, unreachable, connection_lost, failed,
protocol; ``reason`` (optional) is the finer cause (offline, unknown_desk,
not_online, network, not_signed_in, timeout, local, ...); ``desk`` (optional)
the desk it concerned. ``exec``/``shell --json`` keep their whole object and
put the same ``{kind, message, reason}`` in its ``error``.

Exit codes (``gaiadesk-cli --help``):

* exec/shell: 0-255 the remote command's own; 124 ``--timeout`` ran out;
  130 interrupted; 253 (shell) remote error / connection lost; 254 the desk
  refused; 255 gaiadesk-cli's own error.
* desk operations (cp, run, ps, logs, kill, stats, token, audit, ...):
  0 done; 1 ran and did not succeed; 254 refused; 255 own error.
"""

from __future__ import annotations

from typing import Any, Dict, NamedTuple, Optional, Sequence


class GaiaDeskError(Exception):
    """Base class. ``exit_code``, ``kind``, ``reason``, ``desk``, ``stderr``,
    ``argv`` and ``json`` describe the failure.

    ``kind`` is the finer cause when the CLI gave one (``offline``,
    ``unknown_desk``, ...), else the envelope's kind (``refused``,
    ``failed``, ...) or an SDK kind (``cli_error``, ``not_found``)."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: Optional[int] = None,
        kind: str = "cli_error",
        stderr: str = "",
        argv: Sequence[str] = (),
        json: Any = None,
        reason: Optional[str] = None,
        desk: Optional[str] = None,
        request_id: Optional[str] = None,
        status: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.kind = kind
        self.stderr = stderr
        self.argv = list(argv)
        self.json = json
        self.reason = reason
        """The finer cause the CLI gave (``offline``, ``timeout``, ...), when it gave one."""
        self.desk = desk
        """The desk the failure concerned, when the CLI said."""
        self.request_id = request_id
        """API transport: the failed request's id (``req_…``), to quote to support; else None."""
        self.status = status
        """API transport: the HTTP status of the failed request; else None."""
        self.retry_after = retry_after
        """API transport: seconds to wait before retrying (a 429's ``Retry-After``); else None."""


class CliNotFoundError(GaiaDeskError):
    """gaiadesk-cli could not be started (not installed, wrong path)."""


class UsageError(GaiaDeskError):
    """Bad arguments (kind ``usage``), caught by the SDK or by gaiadesk-cli;
    also an option the installed gaiadesk-cli is too old for (e.g. ``cwd``)."""


class RefusedError(GaiaDeskError):
    """The desk said no: wrong code, a token without the scope, expired/revoked, permission off,
    a ``cwd`` outside a confined token's folder (exit 254)."""


class UnreachableError(GaiaDeskError):
    """The desk could not be reached: kind ``unreachable``; ``kind``/``reason`` say which
    (offline, unknown_desk, not_online, network, not_signed_in, timeout)."""


class ConnectionLostError(GaiaDeskError):
    """The connection went away mid-command (kind ``connection_lost``, shell exit 253)."""


class OperationFailedError(GaiaDeskError):
    """A desk operation ran and did not succeed (kind ``failed``, exit 1): a file failed to copy, no such job, ..."""


class ProtocolError(GaiaDeskError):
    """gaiadesk-cli printed something that is not the JSON it documents, or
    (kind ``protocol``) the desk answered something the CLI can't use,
    usually a GaiaDesk too old for the request."""


class EndToEndError(GaiaDeskError):
    """API transport: an operation could not be end-to-end encrypted, or its sealed answer did not
    open (kind ``e2e``). ``reason`` says which: ``e2e_unavailable`` (the desk publishes no key, or
    the ``cryptography`` package is missing, where encryption is required), ``e2e_key_mismatch``
    (the server handed out a key other than the one pinned with ``e2e_keys``),
    ``e2e_decrypt_failed`` / ``e2e_malformed`` (an answer was altered, reordered or not sealed).
    Nothing is sent in the clear when this is raised before a request."""


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


ADMIN_NOT_VIA_API = "admin_not_via_api"
"""The reason (kind ``refused``) the hosted API and a desk's local and LAN APIs give for
administrator work: an exec asking to run as administrator (an ExecResult with exit 254 and
this ``error``) or minting a token with the ``admin`` scope (403). Administrator work runs
only through ``gaiadesk-cli exec --admin``."""

KINDS = ("usage", "refused", "unreachable", "connection_lost", "failed", "protocol")
"""The six kinds of the CLI's error envelope."""

_UNREACHABLE = {"offline", "unknown_desk", "not_online", "network", "not_signed_in", "timeout"}

# Finer causes (the envelope's ``reason``) that become the SDK error's ``kind``.
REASONS = frozenset(_UNREACHABLE | {"connection_lost", "local", "interrupted"})

_CLASSES = {
    "usage": UsageError,
    "refused": RefusedError,
    "unreachable": UnreachableError,
    "connection_lost": ConnectionLostError,
    "failed": OperationFailedError,
    "protocol": ProtocolError,
}


def error_class(kind: str) -> type:
    """The class for an envelope kind."""
    return _CLASSES.get(kind, GaiaDeskError)


def sdk_kind(kind: str, reason: Optional[str]) -> str:
    """The SDK error's ``kind``: the finer reason when it is a known one, else the kind."""
    return reason if isinstance(reason, str) and reason in REASONS else kind


def error_for_kind(kind: str, message: str, reason: Optional[str] = None, **details: Any) -> GaiaDeskError:
    details["kind"] = sdk_kind(kind, reason)
    details["reason"] = reason
    err: GaiaDeskError = error_class(kind)(message, **details)
    return err


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

    kind: str
    """One of ``KINDS``."""
    message: str
    reason: Optional[str] = None
    """The finer cause, when there is one."""
    desk: Optional[str] = None
    """The desk it concerned, when the CLI said."""


def _opt_str(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and v else None


def error_envelope(parsed: Any) -> Optional[ErrorEnvelope]:
    """THE place that knows how gaiadesk-cli spells an error in its JSON:
    ``{"error": {"kind", "message", "reason"?, "desk"?}}`` (every --json
    failure, and the "error" of exec/shell's object). Every error path in the
    SDK goes through here.

    Returns None when the JSON is not an error (including exec's own
    ``"error": null`` on success).
    """
    if not isinstance(parsed, dict):
        return None
    e = parsed.get("error")
    if not isinstance(e, dict) or not isinstance(e.get("kind"), str):
        return None
    m = e.get("message")
    return ErrorEnvelope(e["kind"], m if isinstance(m, str) else "", _opt_str(e.get("reason")), _opt_str(e.get("desk")))


def error_from_run(code: Optional[int], stderr: str, argv: Sequence[str], parsed: Any) -> GaiaDeskError:
    """The typed error for a failed run: the JSON's error envelope (its kind,
    reason and desk), else the exit code with the best message available (a
    ``{"desk", "ok": False, "message"}`` reply, or stderr)."""
    env = error_envelope(parsed)
    desk = env.desk if env is not None else None
    if desk is None and isinstance(parsed, dict):
        desk = _opt_str(parsed.get("desk"))
    details: Dict[str, Any] = dict(exit_code=code, stderr=stderr, argv=argv, json=parsed, desk=desk)
    if env is not None:
        return error_for_kind(env.kind, env.message or last_stderr_line(stderr) or env.kind, env.reason, **details)
    msg = ""
    if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
        msg = parsed["message"]
    msg = msg or last_stderr_line(stderr) or "gaiadesk-cli exited with %s" % code
    return error_for_exit(code, msg, **details)


def exec_outcome(r: Dict[str, Any], check: bool, code: Optional[int], stderr: str, args: Sequence[str]) -> Any:
    """An exec/shell result (or exit event): returned, or the error it means."""
    details: Dict[str, Any] = dict(exit_code=code, stderr=stderr, argv=args, json=r)
    env = error_envelope(r)
    # The command never ran (or the connection went): an error with a kind.
    # ``failed`` is the command's own failure (could not start, stopped,
    # timed out): a result, unless nothing ran at all.
    if env is not None:
        never_ran = r.get("remote_code") is None and r["exit"] == 255
        if env.kind != "failed" or never_ran:
            raise error_from_run(code, stderr, args, r)
    if check and r["exit"] != 0:
        why = "timed out" if r.get("timed_out") else "exited %d" % r["exit"]
        details["kind"] = "failed"
        raise CommandError("command on desk %s %s" % (r.get("desk"), why), r, **details)
    return r


def last_stderr_line(stderr: str) -> str:
    """The last thing gaiadesk-cli said on stderr, without its ``gaiadesk-cli: `` prefix."""
    lines = [l.strip() for l in stderr.splitlines()]
    lines = [l for l in lines if l and not l.startswith("(see `gaiadesk-cli")]
    last = lines[-1] if lines else ""
    return last[len("gaiadesk-cli:"):].strip() if last.startswith("gaiadesk-cli:") else last
