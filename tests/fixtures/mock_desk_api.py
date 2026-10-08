"""A desk serving ``/v1`` itself, for the ``local`` and ``lan`` transports: the
mock hosted API's handler (mock_api.py, answering from the fake CLI) behind
the desk's own doors.

* ``MockLocalApi``: HTTP/1.1 on a Unix socket in a fresh directory (its
  ``dir`` is what ``GAIADESK_API_DIR`` points at), with the local admin token
  written to ``api-token`` there. Accepts ``Authorization: Bearer <that
  token>`` or an agent token in ``X-GaiaDesk-Desk-Token``.
* ``MockLanApi``: HTTPS on 127.0.0.1 with the self-signed fixture certificate
  (lan-cert.pem / lan-key.pem). Accepts agent tokens only; the admin token is
  401 ``admin_token_local_only``.

Credentials are mapped onto the hosted mock's: the admin token, or the agent
token ``gdagt_owner``, is the desk's owner (may administer tokens); any other
agent token is a scoped token (``ak_`` + the desk token, so token routes are
refused). Each request is recorded with its ``host`` header.
"""

import hashlib
import os
import socketserver
import ssl
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_api import Handler, request_id  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LAN_CERT = os.path.join(HERE, "lan-cert.pem")
LAN_KEY = os.path.join(HERE, "lan-key.pem")
ADMIN_TOKEN = "gdlocal_" + "0123456789abcdef" * 4


def lan_fingerprint():
    """The fixture certificate's SHA-256, as 64 lowercase hex digits."""
    with open(LAN_CERT) as f:
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(f.read())).hexdigest()


class DeskHandler(Handler):
    server_version = "MockGaiaDeskDesk/1"

    def handle_api(self, rec):
        rec["host"] = self.headers.get("Host")
        h = rec["headers"]
        auth = h["authorization"] or ""
        desk_token = h["x-gaiadesk-desk-token"] or ""
        lan = self.server.mode == "lan"
        if lan and auth:
            rid = request_id()
            return self.send_json(401, {"error": {"kind": "refused", "reason": "admin_token_local_only", "request_id": rid,
                                                  "message": "the LAN gateway takes agent tokens only (X-GaiaDesk-Desk-Token)"}})
        if desk_token == "gdagt_owner" or (not lan and not desk_token and auth == "Bearer " + ADMIN_TOKEN):
            mapped = dict(h, authorization="Bearer session-owner", **{"x-gaiadesk-desk-token": None})
        elif desk_token.startswith("gdagt_"):
            mapped = dict(h, authorization="Bearer ak_desk")
        else:
            return self.send_error_env({"kind": "refused", "reason": "unauthenticated",
                                        "message": "send the local admin token, or an agent token in X-GaiaDesk-Desk-Token"})
        return Handler.handle_api(self, dict(rec, headers=mapped))


class _Quiet:
    def handle_error(self, request, client_address):
        """A client that hung up (a pinned-certificate mismatch closes right after the handshake) is no failure here."""


if hasattr(socketserver, "UnixStreamServer"):  # not on Windows, where the local transport is a named pipe

    class _UnixServer(_Quiet, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True


class _TlsServer(_Quiet, ThreadingHTTPServer):
    daemon_threads = True


class _Base:
    def _start(self, server, mode):
        self.server = server
        server.mode = mode
        server.requests = []
        self.requests = server.requests
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def last(self):
        return self.requests[-1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class MockLocalApi(_Base):
    """``dir`` (for ``GAIADESK_API_DIR``), ``socket_path``, ``requests``, ``close()``."""

    def __init__(self):
        # A short directory: a Unix socket path has a ~104-byte limit.
        base = "/tmp" if os.path.isdir("/tmp") else None
        self.dir = tempfile.mkdtemp(prefix="gd-", dir=base)
        self.socket_path = os.path.join(self.dir, "api.sock")
        with open(os.path.join(self.dir, "api-token"), "w") as f:
            f.write(ADMIN_TOKEN + "\n")
        self._start(_UnixServer(self.socket_path, DeskHandler), "local")


class MockLanApi(_Base):
    """``url`` (``https://127.0.0.1:<port>/v1``), ``fingerprint``, ``requests``, ``close()``."""

    def __init__(self):
        server = _TlsServer(("127.0.0.1", 0), DeskHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(LAN_CERT, LAN_KEY)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        self.url = "https://127.0.0.1:%d/v1" % server.server_address[1]
        self.fingerprint = lan_fingerprint()
        self._start(server, "lan")
