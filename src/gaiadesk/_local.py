"""The ``local`` and ``lan`` transports: the same ``/v1`` desk operations as
the hosted API (same routes, JSON results, error envelope, SSE events and
held waits), served by a desk itself. Both are the ``api`` transport
(``_api.ApiTransport``) with another connection and other credentials; the
operations are not repeated here.

* ``local``: code running on the desk talks to its own GaiaDesk, over the
  Unix socket ``$GAIADESK_API_DIR/api.sock`` (else ``~/.gaiadesk/api.sock``)
  or, on Windows, the named pipe ``\\\\.\\pipe\\gaiadesk-api-<user>``
  (``$GAIADESK_API_PIPE``). Credentials: a desk token (``gdagt_…``) as
  ``X-GaiaDesk-Desk-Token``, else the desk's local admin token (``gdlocal_…``,
  from ``api-token`` beside the socket) as ``Authorization: Bearer``.
* ``lan``: a desk's opt-in LAN gateway over HTTPS with a self-signed
  certificate, pinned by its SHA-256 fingerprint (checked right after the
  handshake, before any request byte is sent). Agent tokens only.
"""

from __future__ import annotations

import getpass
import hashlib
import http.client
import io
import os
import re
import socket
import ssl
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ._api import ApiTransport, check_desk_token
from .errors import GaiaDeskError, UnreachableError, UsageError

LOCAL_API_UNAVAILABLE = ("GaiaDesk is not serving its local API here: is the app running, "
                         "and is Settings → GaiaDesk API → Local API on?")
PIPE_PREFIX = "\\\\.\\pipe\\"
"""``\\\\.\\pipe\\``: an address starting with it (any case, either slash) is a Windows named pipe."""


class FingerprintMismatchError(UnreachableError):
    """``lan``: the gateway's certificate is not the pinned one (kind ``unreachable``,
    reason ``fingerprint_mismatch``). Nothing was sent to it."""


# ───────────────────────────── pure helpers ─────────────────────────────


def _env(environ: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def local_api_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    """Where the desk's local API lives: ``$GAIADESK_API_DIR`` when it is an absolute
    path, else ``~/.gaiadesk`` (on Windows ``%USERPROFILE%\\.gaiadesk``)."""
    d = _env(environ).get("GAIADESK_API_DIR")
    if d and os.path.isabs(d):
        return d
    return str(Path.home() / ".gaiadesk")


def local_socket_path(environ: Optional[Mapping[str, str]] = None) -> str:
    """The local API's Unix socket: ``<local_api_dir>/api.sock``."""
    return os.path.join(local_api_dir(environ), "api.sock")


def local_token_path(environ: Optional[Mapping[str, str]] = None) -> str:
    """The desk's local admin token file: ``<local_api_dir>/api-token``."""
    return os.path.join(local_api_dir(environ), "api-token")


def pipe_user(user: str) -> str:
    """A user name as the pipe name has it: lowercased, every character outside
    ``[a-z0-9._-]`` replaced by ``_``, at most 64 characters, ``user`` if empty."""
    u = re.sub(r"[^a-z0-9._-]", "_", (user or "").lower())[:64]
    return u or "user"


def local_pipe_name(environ: Optional[Mapping[str, str]] = None) -> str:
    """The local API's Windows named pipe: ``$GAIADESK_API_PIPE``, else
    ``\\\\.\\pipe\\gaiadesk-api-<user>`` (``<user>``: ``%USERNAME%``, else the login name)."""
    env = _env(environ)
    if env.get("GAIADESK_API_PIPE"):
        return env["GAIADESK_API_PIPE"]
    user = env.get("USERNAME") or ""
    if not user:
        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001 (no login name: the pipe says "user")
            user = ""
    return PIPE_PREFIX + "gaiadesk-api-" + pipe_user(user)


def default_local_address(environ: Optional[Mapping[str, str]] = None) -> str:
    """This platform's local API address: the named pipe on Windows, else the Unix socket."""
    return local_pipe_name(environ) if os.name == "nt" else local_socket_path(environ)


def is_pipe(address: str) -> bool:
    return address.replace("/", "\\").lower().startswith(PIPE_PREFIX)


def normalize_fingerprint(fingerprint: str) -> str:
    """A SHA-256 certificate fingerprint as the desk's Settings shows it: 32 lowercase
    hex pairs joined by ``:``. Takes it with or without colons (or spaces), any case."""
    h = re.sub(r"[\s:]", "", fingerprint).lower() if isinstance(fingerprint, str) else ""
    if not re.fullmatch(r"[0-9a-f]{64}", h):
        raise UsageError("fingerprint must be the gateway certificate's SHA-256 fingerprint: 32 hex pairs "
                         "(ab:cd:…, as the desk's Settings shows it), got %r" % (fingerprint,), kind="usage")
    return ":".join(h[i:i + 2] for i in range(0, 64, 2))


def certificate_fingerprint(der: bytes) -> str:
    """The SHA-256 fingerprint of a DER certificate, in ``normalize_fingerprint``'s form."""
    return normalize_fingerprint(hashlib.sha256(der).hexdigest())


# ───────────────────────────── connections ─────────────────────────────


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 over a Unix socket (``Host: localhost``)."""

    def __init__(self, path: str, timeout: Optional[float] = None) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise FileNotFoundError("Unix sockets are not available on this platform: %s" % self.socket_path)
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            if self.timeout is not None and self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
                s.settimeout(self.timeout)
            s.connect(self.socket_path)
        except BaseException:
            s.close()
            raise
        self.sock = s


class _PipeReader(io.RawIOBase):
    """The read side of a pipe for ``makefile()``: closing it leaves the pipe open."""

    def __init__(self, f: Any) -> None:
        super().__init__()
        self._f = f

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> Optional[int]:
        try:
            return self._f.readinto(b)
        except BrokenPipeError:
            return 0  # the server closed its end: the end of the answer


class PipeSocket:
    """A Windows named pipe opened as a file, with the socket methods ``http.client`` uses."""

    def __init__(self, f: Any) -> None:
        self._f = f

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            n = self._f.write(view)
            if not n:
                raise BrokenPipeError("the local API's pipe took no bytes")
            view = view[n:]

    def makefile(self, mode: str = "rb", *a: Any, **kw: Any) -> io.BufferedReader:
        return io.BufferedReader(_PipeReader(self._f))

    def settimeout(self, _t: Any) -> None:
        """Pipes have no timeout here."""

    def shutdown(self, _how: int) -> None:
        """Stop a read blocked on another thread (``ApiStream.kill``), then close."""
        try:
            import ctypes
            import msvcrt

            ctypes.windll.kernel32.CancelIoEx(msvcrt.get_osfhandle(self._f.fileno()), None)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 (best effort)
            pass
        self.close()

    def close(self) -> None:
        try:
            self._f.close()
        except OSError:
            pass


class PipeHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 over a Windows named pipe (``Host: localhost``). Importable anywhere;
    ``connect()`` is for Windows."""

    BUSY_WAIT = 5.0

    def __init__(self, name: str, timeout: Optional[float] = None) -> None:
        super().__init__("localhost", timeout=timeout)
        self.pipe_name = name

    def connect(self) -> None:
        deadline = time.monotonic() + self.BUSY_WAIT
        while True:
            try:
                f = open(self.pipe_name, "r+b", buffering=0)
                break
            except OSError as e:
                # ERROR_PIPE_BUSY (231): every instance is serving someone; the server makes another.
                if getattr(e, "winerror", None) == 231 and time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
                raise
        self.sock = PipeSocket(f)  # type: ignore[assignment]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a self-signed certificate, pinned by its SHA-256 fingerprint: no chain or
    hostname check, but right after the handshake (before a request byte is sent) the
    certificate must be the pinned one, else ``FingerprintMismatchError``."""

    def __init__(self, host: str, port: Optional[int], fingerprint: str, timeout: Optional[float] = None) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        super().__init__(host, port, timeout=timeout, context=ctx)
        self.fingerprint = normalize_fingerprint(fingerprint)

    def connect(self) -> None:
        super().connect()  # TCP and the TLS handshake
        sock: Any = self.sock
        der = sock.getpeercert(binary_form=True) if sock is not None else None
        got = certificate_fingerprint(der) if der else "none"
        if got != self.fingerprint:
            self.close()
            raise FingerprintMismatchError(
                "the desk at %s:%s did not prove the identity you pinned: its certificate's SHA-256 fingerprint is %s, "
                "not the pinned %s (check the fingerprint in the desk's Settings → GaiaDesk API)" % (self.host, self.port, got, self.fingerprint),
                kind="unreachable", reason="fingerprint_mismatch", exit_code=255, argv=["connect"])


# ───────────────────────────── the transports ─────────────────────────────


class LocalTransport(ApiTransport):
    """The desk's own ``/v1``, over its Unix socket or named pipe."""

    transport = "local"

    def __init__(self, desk_token: Optional[str] = None, token: Optional[str] = None, socket_path: Optional[str] = None,
                 timeout: Optional[float] = None, environ: Optional[Mapping[str, str]] = None) -> None:
        self._desk_token = check_desk_token(desk_token)
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise UsageError("token must be a non-empty string (the desk's local admin token, gdlocal_…)", kind="usage")
        if token is not None and self._desk_token is not None:
            raise UsageError("give desk_token (an agent token) or token (the local admin token), not both", kind="usage")
        if socket_path is not None and (not isinstance(socket_path, str) or not socket_path):
            raise UsageError("socket_path must be a non-empty string", kind="usage")
        self._environ = dict(environ) if environ is not None else None
        self._token = token.strip() if token else None
        self.address = socket_path or default_local_address(self._environ)
        self._pipe = is_pipe(self.address)
        self.base_url = ("pipe:" if self._pipe else "unix:") + self.address
        self._prefix = "/v1"
        self._wake = None
        self._timeout = timeout

    def admin_token(self) -> str:
        """The desk's local admin token: ``token=``, else the ``api-token`` file (read on every request: it changes when the app does)."""
        if self._token:
            return self._token
        path = local_token_path(self._environ)
        try:
            with open(path, "r", encoding="utf-8") as f:
                t = f.read().strip()
        except OSError as e:
            raise UnreachableError("%s (no local admin token at %s: %s; or give desk_token=)" % (LOCAL_API_UNAVAILABLE, path, e),
                                   kind="unreachable", reason="local_api_unavailable", exit_code=255, argv=["local"]) from e
        if not t:
            raise UnreachableError("%s (the local admin token file %s is empty)" % (LOCAL_API_UNAVAILABLE, path),
                                   kind="unreachable", reason="local_api_unavailable", exit_code=255, argv=["local"])
        return t

    def headers(self) -> Dict[str, str]:
        """A desk token as ``X-GaiaDesk-Desk-Token`` (no Authorization), else the admin token as Bearer."""
        h = {"User-Agent": "gaiadesk-python"}
        if self._desk_token:
            h["X-GaiaDesk-Desk-Token"] = self._desk_token
        else:
            h["Authorization"] = "Bearer " + self.admin_token()
        return h

    def _connection(self) -> http.client.HTTPConnection:
        if self._pipe:
            return PipeHTTPConnection(self.address, self._timeout)
        return UnixHTTPConnection(self.address, self._timeout)

    def _network_error(self, e: BaseException, op: str) -> GaiaDeskError:
        if isinstance(e, (FileNotFoundError, ConnectionRefusedError)):
            return UnreachableError("%s (nothing listening at %s)" % (LOCAL_API_UNAVAILABLE, self.address), kind="unreachable",
                                    reason="local_api_unavailable", exit_code=255, argv=[op])
        return UnreachableError("GaiaDesk's local API (%s) could not be reached: %s" % (self.address, e), kind="network",
                                reason="network", exit_code=255, argv=[op])


class LanTransport(ApiTransport):
    """A desk's LAN gateway (``https://<desk>:7443/v1``), its certificate pinned."""

    transport = "lan"

    def __init__(self, base_url: str, fingerprint: str, desk_token: Optional[str], timeout: Optional[float] = None) -> None:
        if not isinstance(base_url, str) or not base_url:
            raise UsageError("the lan transport needs base_url (https://<desk>:7443/v1, from the desk's Settings)", kind="usage")
        self._set_base(base_url, ("https",), "the lan transport's base_url must be an https:// URL: %r" % (base_url,))
        if fingerprint is None:
            raise UsageError("the lan transport needs fingerprint (the gateway certificate's SHA-256, from the desk's Settings)", kind="usage")
        self.fingerprint = normalize_fingerprint(fingerprint)
        self._desk_token = check_desk_token(desk_token)
        if self._desk_token is None:
            raise UsageError("the lan transport needs desk_token (a scoped agent token, gdagt_…): the LAN gateway takes agent tokens only",
                             kind="usage")
        self._wake = None
        self._timeout = timeout

    def headers(self) -> Dict[str, str]:
        """The agent token as ``X-GaiaDesk-Desk-Token``; no Authorization."""
        return {"User-Agent": "gaiadesk-python", "X-GaiaDesk-Desk-Token": self._desk_token or ""}

    def _connection(self) -> http.client.HTTPConnection:
        return PinnedHTTPSConnection(self._host, self._port, self.fingerprint, self._timeout)


__all__ = [
    "FingerprintMismatchError",
    "LanTransport",
    "LocalTransport",
    "certificate_fingerprint",
    "default_local_address",
    "local_api_dir",
    "local_pipe_name",
    "local_socket_path",
    "local_token_path",
    "normalize_fingerprint",
    "pipe_user",
]
