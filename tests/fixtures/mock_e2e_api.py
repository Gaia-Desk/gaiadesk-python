"""mock_api with desks that open END-TO-END ENCRYPTED operations: the API and the desk
in one. A desk configured in ``server.desks`` holds a static X25519 secret and lists
its key on ``GET /desks/{id}``; a sealed request (``{"e2e": …}`` body or the
``GaiaDesk-E2E`` header) is opened as the desk would (route's op, age, replay),
turned into the plaintext request it carries, answered by mock_api's own handler
(the fake CLI), and that answer sealed back as the desk's events:

* a JSON answer: ``{"e2e": {"v": 1, "events": [...]}}`` (exec: a stdout event, then exit);
* a desk error: the envelope with a placeholder message and ``e2e.events``;
* a held wait: ``GaiaDesk-Held: 1``, spaces, then that JSON (or the envelope with ``status``);
* a stream: SSE ``sealed`` events (stdout chunks split mid-character);
* a download: NDJSON of sealed events (the bytes in two chunks, then exit).

Every request is recorded raw in ``server.raw`` (request line, headers, body) so the
tests can check nothing in the clear reached "the server". Per desk config:
``secret`` (bytes), ``listed`` (publish the key), ``required`` (refuse plaintext 409
``e2e_required``), ``list_on_refusal`` / ``list_on_wake`` (start listing then), ``hide_required`` (list
``e2e_required: false`` all the same),
``advertise`` (a key to list in place of the real one).
"""

import base64
import json
import time
from io import BytesIO

from e2e_desk import open_request
from mock_api import Handler, MockApi, request_id

from gaiadesk import _e2e as E

PLACEHOLDER = "the desk's error is end-to-end encrypted"

_ROUTE_OPS = [("POST", "/exec", "exec"), ("POST", "/jobs", "job_start"), ("GET", "/jobs", "job_list"), ("GET", "/stats", "stats"),
              ("PUT", "/files", "file_put"), ("GET", "/files", "file_get"), ("POST", "/tokens", "token_mint"),
              ("GET", "/tokens", "token_list")]


def route_op(method, rest):
    for m, r, op in _ROUTE_OPS:
        if m == method and rest == r:
            return op
    parts = rest.split("/")
    if len(parts) >= 3 and parts[1] == "jobs":
        if len(parts) == 3 and method == "DELETE":
            return "job_kill"
        if parts[3:] == ["logs"] and method == "GET":
            return "job_logs"
        if parts[3:] == ["wait"] and method == "GET":
            return "job_wait"
    if len(parts) == 3 and parts[1] == "tokens" and method == "DELETE":
        return "token_revoke"
    return None


def plaintext_of(desk, req):
    """The plaintext HTTP request a sealed operation stands for: (method, path, query, body)."""
    base = "/v1/desks/%s" % desk
    op = req["op"]
    if op == "exec":
        return "POST", base + "/exec", ({"stream": "1"} if req.get("stream") else {}), json.dumps(req["spec"]).encode()
    if op == "job_start":
        return "POST", base + "/jobs", {}, json.dumps(req["spec"]).encode()
    if op == "token_mint":
        return "POST", base + "/tokens", {}, json.dumps(req["spec"]).encode()
    if op == "job_list":
        return "GET", base + "/jobs", {}, b""
    if op == "token_list":
        return "GET", base + "/tokens", {}, b""
    if op == "stats":
        return "GET", base + "/stats", {}, b""
    if op == "job_kill":
        return "DELETE", base + "/jobs/" + req["name"], {}, b""
    if op == "token_revoke":
        return "DELETE", base + "/tokens/" + req["token"], {}, b""
    if op == "job_logs":
        q = {}
        if req.get("tail") is not None:
            q["tail"] = str(req["tail"])
        if req.get("follow"):
            q["follow"] = "1"
        return "GET", base + "/jobs/%s/logs" % req["name"], q, b""
    if op == "job_wait":
        return "GET", base + "/jobs/%s/wait" % req["name"], {"timeout": str(req["timeout_ms"] // 1000)}, b""
    if op == "file_put":
        return "PUT", base + "/files", {"path": req["path"]}, None
    if op == "file_get":
        return "GET", base + "/files", {"path": req["path"]}, b""
    raise ValueError(op)


def parse_response(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for l in lines[1:]:
        k, _, v = l.partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, body


def b64(b):
    return base64.b64encode(b).decode("ascii")


def halves(b):
    """Bytes in two pieces, cut in the middle (mid-character for multi-byte text)."""
    cut = max(1, len(b) // 2)
    return [x for x in (b[:cut], b[cut:]) if x]


class E2eHandler(Handler):
    server_version = "MockGaiaDeskE2E/1"

    def route(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.server.raw.append(self.requestline.encode() + b"\n" + str(self.headers).encode() + b"\n" + body)
        parts = self.path.split("?")[0].split("/")
        desk = parts[3] if len(parts) > 3 else ""
        cfg = self.server.desks.get(desk)
        if cfg is None:
            self.rfile = BytesIO(body)
            return super().route()
        from urllib.parse import parse_qsl, urlsplit

        u = urlsplit(self.path)
        rec = {"method": self.command, "path": u.path, "query": dict(parse_qsl(u.query)), "body": body,
               "headers": {k.lower(): self.headers.get(k) for k in ("Authorization", "X-GaiaDesk-Desk-Token", "Content-Type", "Accept", "GaiaDesk-E2E")}}
        self.server.requests.append(rec)
        rest = "/" + "/".join(parts[4:]) if len(parts) > 4 else ""
        try:
            self.e2e_route(desk, cfg, rest, rec)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # the caller hung up (a stream it refused to read on)
        except Exception as e:  # noqa: BLE001
            self.send_error_env({"kind": "protocol", "message": "mock e2e API: %r" % e})

    def e2e_route(self, desk, cfg, rest, rec):
        if rest == "" and self.command == "GET":
            info = {"desk_id": desk, "online": True, "features": ["desk_op", E.FEATURE], "e2e_required": bool(cfg.get("required")) and not cfg.get("hide_required")}
            if cfg.get("listed"):
                info["e2e_pub"] = cfg.get("advertise") or E.b64encode(E.public_key(cfg["secret"]))
            return self.send_json(200, info)
        if rest == "/wake" and self.command == "POST":
            self.server.wakes.append(desk)
            if cfg.get("list_on_wake"):
                cfg["listed"] = True
            return self.send_json(200, {"rang": [], "woke": False, "online": True})
        envelope = None
        if self.command == "POST" and rec["body"]:
            b = json.loads(rec["body"])
            envelope = b.get("e2e") if isinstance(b, dict) else None
        if rec["headers"]["gaiadesk-e2e"]:
            envelope = json.loads(E.b64decode(rec["headers"]["gaiadesk-e2e"]))
        if envelope is None:
            if cfg.get("required"):
                if cfg.get("list_on_refusal"):
                    cfg["listed"] = True
                return self.send_error_env({"kind": "refused", "reason": "e2e_required", "desk": desk,
                                            "message": "this desk requires end-to-end encryption"}, status=409)
            return self.handle_api(rec)
        return self.sealed(desk, cfg, rest, rec, envelope)

    def send_error_env(self, error, extra=None, status=None):
        if status is None:
            return super().send_error_env(error, extra)
        self.send_json(status, {"error": dict(error, request_id=request_id())}, extra)

    def refuse(self, desk, reason):
        self.send_error_env({"kind": "refused", "reason": reason, "desk": desk, "message": "refused: %s" % reason})

    def sealed(self, desk, cfg, rest, rec, envelope):
        op = route_op(self.command, rest)
        try:
            plain, seal = open_request(cfg["secret"], desk, op, envelope)
        except E.OpenError as e:
            return self.refuse(desk, e.reason)
        inner = json.loads(plain)
        if envelope["pub"] in self.server.seen:
            return self.refuse(desk, "e2e_replayed")
        self.server.seen.add(envelope["pub"])
        if abs(inner["ts"] - time.time()) > 600:
            return self.refuse(desk, "e2e_stale")
        req = inner["request"]
        if req.get("op") != op:
            return self.refuse(desk, "e2e_op_mismatch")
        self.server.opened.append(req)
        method, path, query, body = plaintext_of(desk, req)
        if body is None:  # an upload: its sealed input frames, in order
            data, last = b"", False
            for line in rec["body"].splitlines():
                if line.strip():
                    if last:
                        return self.refuse(desk, "e2e_malformed")
                    last, chunk = seal.open_input(json.loads(line))
                    data += chunk
            if not last:
                return self.send_error_env({"kind": "failed", "message": "the upload ended early"})
            body = data
            self.server.uploaded.append(data)
        prec = {"method": method, "path": path, "query": query, "body": body, "headers": dict(rec["headers"])}
        status, headers, out = self.capture(prec)
        self.answer(desk, req, seal, status, headers, out)

    def capture(self, prec):
        real = self.wfile
        self.wfile = BytesIO()
        try:
            self.handle_api(prec)
            if hasattr(self, "_headers_buffer") and self._headers_buffer:
                self.flush_headers()
            raw = self.wfile.getvalue()
        finally:
            self.wfile = real
        self.close_connection = True
        return parse_response(raw)

    def error_event(self, error):
        e = {"event": "error", "kind": error["kind"], "message": error["message"]}
        if error.get("reason"):
            e["reason"] = error["reason"]
        return e

    def answer(self, desk, req, seal, status, headers, out):
        ctype = headers.get("content-type", "")
        if status >= 400:
            env = json.loads(out)
            ev = seal.seal_event(self.error_event(env["error"]))
            err = dict(env["error"], message=PLACEHOLDER)
            return self.send_json(status, {"error": err, "e2e": {"v": 1, "events": [ev]}})
        if ctype.startswith("text/event-stream"):
            return self.answer_stream(req, seal, out)
        if ctype == "application/octet-stream":
            frames = [seal.seal_event({"event": "stdout", "data": b64(p)}) for p in halves(out)]
            frames.append(seal.seal_event({"event": "exit", "result": {"direction": "download", "files": 1, "bytes": len(out), "failed": []}}))
            if self.server.truncate_download:
                frames = frames[:-1]
            body = "".join(json.dumps(f) + "\n" for f in frames).encode()
            self.send_response(200)
            self.send_header("Content-Type", E.FRAMES_CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        value = json.loads(out)
        events = []
        if isinstance(value, dict) and "error" in value and isinstance(value["error"], dict) and "kind" in value["error"] and "exit" not in value:
            events.append(seal.seal_event(self.error_event(value["error"])))
            answer = {"error": dict(value["error"], message=PLACEHOLDER), "e2e": {"v": 1, "events": events}}
        else:
            if req["op"] == "exec" and isinstance(value.get("stdout"), str) and value["stdout"]:
                events.append(seal.seal_event({"event": "stdout", "data": b64(value["stdout"].encode())}))
            events.append(seal.seal_event({"event": "exit", "result": value}))
            answer = {"e2e": {"v": 1, "events": events}}
        if self.server.tamper_answers:
            f = answer["e2e"]["events"][-1]
            c = bytearray(E.b64decode(f["ciphertext"]))
            c[0] ^= 1
            f["ciphertext"] = E.b64encode(bytes(c))
        payload = json.dumps(answer).encode()
        if headers.get("gaiadesk-held") == "1":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("GaiaDesk-Held", "1")
            self.end_headers()
            self.wfile.write(b"   " + payload)
            return None
        self.send_json(status, answer)

    def answer_stream(self, req, seal, out):
        logs = req["op"] == "job_logs"
        desk_events = []
        for ev in self.parse_sse(out):
            name = ev.get("event")
            if name in ("stdout", "stderr", "output"):
                for p in halves(ev["data"].encode()):
                    desk_events.append({"event": "stderr" if name == "stderr" else "stdout", "data": b64(p)})
            elif name == "exit" and not logs:
                desk_events.append({"event": "exit", "result": {k: v for k, v in ev.items() if k != "event"}})
            elif name == "end":
                desk_events.append({"event": "exit", "result": {"job": ev.get("job")}})
            elif name == "interrupted":
                desk_events.append({"event": "exit", "result": {"interrupted": True}})
            elif name == "error":
                desk_events.append(self.error_event(ev["error"]))
        self.start_sse()
        if self.server.plain_in_stream:
            self.sse("stdout", json.dumps({"event": "stdout", "data": "injected"}))
        for d in desk_events:
            f = seal.seal_event(d)
            self.sse("sealed", json.dumps(dict(f, event="sealed")))

    @staticmethod
    def parse_sse(out):
        evs = []
        for block in out.decode().replace("\r\n", "\n").split("\n\n"):
            for line in block.split("\n"):
                if line.startswith("data: "):
                    evs.append(json.loads(line[6:]))
        return evs


class MockE2eApi(MockApi):
    """``MockApi`` whose ``desks`` (desk id -> config) answer sealed operations."""

    def __init__(self):
        super().__init__(E2eHandler)
        self.server.raw = self.raw = []
        self.server.desks = self.desks = {}
        self.server.seen = set()
        self.server.opened = self.opened = []
        self.server.wakes = self.wakes = []
        self.server.uploaded = self.uploaded = []
        self.server.tamper_answers = False
        self.server.truncate_download = False
        self.server.plain_in_stream = False
