"""A raw TCP "HTTP server" with no framework in between, for the ways a real
server or proxy fails: accept a request and close the socket before any
response byte (FIN or RST, with or without reading the body), answer the
headers and part of the body and then go silent with the socket open, or
never answer at all. It proves what the SDK does on the wire itself, not what
a test harness happens to do."""

import socket
import struct
import threading
from collections import Counter

CLOSE_BEFORE_RESPONSE = "close_before_response"
"""Read the request's headers, then close before any response byte, leaving the body unread."""
RESET_BEFORE_RESPONSE = "reset_before_response"
"""Read the headers, then reset the connection (RST, SO_LINGER 0) before any response byte."""
CLOSE_AFTER_BODY = "close_after_body"
"""Read the whole request (headers and its Content-Length or chunked body), then close before any response byte."""
STALL_MID_BODY = "stall_mid_body"
"""Send 200 headers and one chunk of a chunked body, then nothing, with the socket left open."""
STALL_MID_JSON = "stall_mid_json"
"""Send 200 headers with a Content-Length larger than what follows, then nothing, the socket open."""
STALL_MID_EVENTS = "stall_mid_events"
"""Send 200 text/event-stream headers and one stdout event, then nothing, the socket open."""
SILENT = "silent"
"""Read the request and never answer."""
KEEP_ALIVE_THEN_CLOSE = "keep_alive_then_close"
"""Answer the first request on a connection 200 (keep-alive), then close on the next one without answering it."""
OK = "ok"
"""Answer 200 with a small JSON result (an ExecResult and a CopyResult in one), then close."""

_OK_BODY = b'{"exit":0,"stdout":"","stderr":"","files":1,"dirs":0,"bytes":0,"failed":[]}'


def status(code, retry_after=None, reason=None, keep_alive=False):
    """A mode: answer every request with this status and the API's error envelope."""
    return ("status", code, retry_after, reason, keep_alive)


def _status_answer(code, retry_after, reason, keep_alive):
    kind = {429: "refused", 409: "refused"}.get(code, "unreachable")
    env = {"error": {"kind": kind, "reason": reason or "", "message": "HTTP %d" % code, "request_id": "req_t"}}
    import json
    body = json.dumps(env).encode()
    head = "HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\n" % (code, len(body))
    if retry_after is not None:
        head += "Retry-After: %s\r\n" % retry_after
    head += "Connection: %s\r\n\r\n" % ("keep-alive" if keep_alive else "close")
    return head.encode() + body

_EVENT = b'event: stdout\ndata: {"event":"stdout","data":"hi"}\n\n'
_ANSWERS = {
    STALL_MID_BODY: b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n",
    STALL_MID_JSON: b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"desk":',
    STALL_MID_EVENTS: b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                      + b"%x\r\n" % len(_EVENT) + _EVENT + b"\r\n",
}


class RawServer:
    """``mode`` may be changed at any time (it applies to the next request); bytes as the mode
    are sent as the answer, then nothing, the socket held open.
    ``count(method)``: the requests received with that method."""

    def __init__(self, mode=CLOSE_BEFORE_RESPONSE, port=0):
        self.mode = mode
        self._counts = Counter()
        self.heads = []
        """Every request's head (request line and header fields), in order."""
        self._lock = threading.Lock()
        self._held = []
        self._stopped = False
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", port))
        self._listener.listen(128)
        self._listener.settimeout(0.1)  # the accept loop sees close() promptly
        self.url = "http://127.0.0.1:%d/v1" % self._listener.getsockname()[1]
        self._accept = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept.start()

    def _count(self, head):
        with self._lock:
            self._counts[head.split(" ", 1)[0]] += 1
            self.heads.append(head)

    def count(self, method):
        with self._lock:
            return self._counts[method]

    def _accept_loop(self):
        while not self._stopped:
            try:
                c, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            c.settimeout(None)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    @staticmethod
    def _read_head(c):
        buf = b""
        while b"\r\n\r\n" not in buf:
            data = c.recv(65536)
            if not data:
                return None, b""
            buf += data
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head.decode("latin-1"), rest

    @staticmethod
    def _read_body(c, head, rest):
        """The rest of the request's body: Content-Length bytes, or chunked up to its last chunk."""
        lines = head.split("\r\n")[1:]
        fields = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines)}
        if fields.get("transfer-encoding", "").lower() == "chunked":
            while not rest.endswith(b"0\r\n\r\n"):
                data = c.recv(65536)
                if not data:
                    return
                rest = (rest + data)[-16:]
            return
        left = int(fields.get("content-length", "0")) - len(rest)
        while left > 0:
            data = c.recv(65536)
            if not data:
                return
            left -= len(data)

    def _serve(self, c):
        try:
            head, rest = self._read_head(c)
            if head is None:
                c.close()
                return
            self._count(head)
            mode = self.mode
            if isinstance(mode, tuple) and mode[0] == "status":
                self._read_body(c, head, rest)
                c.sendall(_status_answer(*mode[1:]))
                while mode[4]:  # keep-alive: the same answer to every request on the connection
                    head, rest = self._read_head(c)
                    if head is None:
                        break
                    self._count(head)
                    self._read_body(c, head, rest)
                    c.sendall(_status_answer(*mode[1:]))
                c.close()
                return
            if mode == OK:
                self._read_body(c, head, rest)
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                          % len(_OK_BODY) + _OK_BODY)
                c.close()
                return
            if mode == KEEP_ALIVE_THEN_CLOSE:
                self._read_body(c, head, rest)
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: keep-alive\r\n\r\n{\"jobs\":[]}")
                head, rest = self._read_head(c)
                if head is not None:
                    self._count(head)
                c.close()
                return
            if mode == CLOSE_BEFORE_RESPONSE:
                c.close()
                return
            if mode == RESET_BEFORE_RESPONSE:
                c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                c.close()
                return
            if mode == CLOSE_AFTER_BODY:
                self._read_body(c, head, rest)
                c.close()
                return
            if isinstance(mode, bytes):
                c.sendall(mode)
            elif mode in _ANSWERS:
                c.sendall(_ANSWERS[mode])
            with self._lock:  # held open, silent, until the server stops
                if self._stopped:
                    c.close()
                else:
                    self._held.append(c)
        except OSError:
            c.close()

    def close(self):
        with self._lock:
            self._stopped = True
            held, self._held = self._held, []
        try:
            self._listener.close()
        except OSError:
            pass
        for c in held:
            try:
                c.close()
            except OSError:
                pass
        self._accept.join(5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
