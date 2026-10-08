"""A mock of GaiaDesk's hosted API (phase 2 desk operations) for the tests:
``http.server`` on a thread. It answers every route by running the fake
gaiadesk-cli (fake_cli.py) with the matching arguments, so the API transport
sees exactly the data the CLI transport does, as the real API relays the
desk's answers in the CLI's JSON shapes:

    GET    /desks                          devices --json
    GET    /desks/{id}                     the desk (online, no end-to-end key)
    POST   /desks/{id}/exec[?stream=1]     exec --json | --json-stream (as SSE)
    POST   /desks/{id}/jobs                run --detach --json            201
    GET    /desks/{id}/jobs                ps --json
    DELETE /desks/{id}/jobs/{name}         kill --json
    GET    /desks/{id}/jobs/{name}/logs    logs --json [--tail] [--follow -> SSE]
    GET    /desks/{id}/stats               stats --json
    PUT    /desks/{id}/files?path=         cp --json <the body as a file> <id>:<path>
    GET    /desks/{id}/files?path=         cp --json (for errors), then the bytes
    POST   /desks/{id}/tokens              token create --json            201
    GET    /desks/{id}/tokens              token list --json
    DELETE /desks/{id}/tokens/{token_id}   token revoke --json

Failures are the API's envelope with a request id, the status by kind (400
usage, 401/403/429 refused, 409/504 unreachable, 422 failed, 502
connection_lost/protocol). ``Authorization: Bearer <key>`` is required; a
key starting ``ak_`` is an API key, which needs X-GaiaDesk-Desk-Token for
desk operations and may not administer tokens; anything else is a person's
session (token routes run with the desk's password, as ``code=`` does on the
CLI). Streams are written in small pieces with keep-alive comments.

Desk ids only the API has: 999999990 answers HTML (no envelope), 999999991
is rate limited (429, Retry-After: 7).
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_cli.py")
HTML_DESK, LIMITED_DESK = "999999990", "999999991"

_seq = [0]
_seq_lock = threading.Lock()


def request_id():
    with _seq_lock:
        _seq[0] += 1
        return "req_%024x" % _seq[0]


def _env(code=None):
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
    if code:
        env["GAIADESK_CODE"] = code
    return env


def run_fake(args, input="", cwd=None, code=None):
    p = subprocess.run([sys.executable, FAKE] + args, input=(input or "").encode("utf-8"), capture_output=True, env=_env(code), cwd=cwd)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def stream_fake(args, on_line, input=""):
    with subprocess.Popen([sys.executable, FAKE] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env()) as p:
        p.stdin.write((input or "").encode("utf-8"))
        p.stdin.close()
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                on_line(line)
        err = p.stderr.read().decode("utf-8", "replace")
        return p.wait(), "", err


def status_for(kind, reason=None):
    if kind == "usage":
        return 400
    if kind == "refused":
        return {"unauthenticated": 401, "rate_limited": 429}.get(reason, 403)
    if kind == "unreachable":
        return 504 if reason == "timeout" else 409
    if kind == "failed":
        return 422
    return 502


WAITS = []
"""Every ``timeout`` a wait was asked with, in order (the tests read it)."""


def env_args(env):
    """A spec's ``env`` as the CLI's ``--env KEY=VALUE`` flags (the fake logs its argv)."""
    if not isinstance(env, dict):
        return []
    out = []
    for k, v in env.items():
        out += ["--env", "%s=%s" % (k, v)]
    return out


def last_line(s):
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    last = lines[-1] if lines else ""
    return last[len("gaiadesk-cli:"):].strip() if last.startswith("gaiadesk-cli:") else last


def parse(s):
    t = s.strip()
    if not t:
        return None
    try:
        return json.loads(t)
    except ValueError:
        try:
            return json.loads(t.splitlines()[-1])
        except ValueError:
            return None


def plain_error(code, stderr):
    """A CLI failure told only on stderr, as the API would report it."""
    message = last_line(stderr) or "exit %s" % code
    if code == 254:
        return {"kind": "refused", "message": message}
    if code == 1:
        return {"kind": "failed", "message": message}
    if code == 0:
        return {"kind": "protocol", "message": "the desk answered something that is not JSON"}
    return {"kind": "unreachable", "message": message}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockGaiaDeskAPI/1"

    def log_message(self, *a):  # quiet
        pass

    # replies

    def send_json(self, status, value, extra=None):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", request_id())
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_error_env(self, error, extra=None):
        rid = request_id()
        self.send_json(status_for(error["kind"], error.get("reason")), {"error": dict(error, request_id=rid)}, extra)

    def relay(self, ran, ok=(0,), status=200):
        code, out, err = ran
        j = parse(out)
        if isinstance(j, dict) and isinstance(j.get("error"), dict):
            return self.send_error_env(j["error"])
        if code in ok and j is not None:
            return self.send_json(status, j)
        self.send_error_env(plain_error(code, err))

    def start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("X-Request-Id", request_id())
        self.end_headers()

    def sse(self, name, data):
        """One event in pieces (split mid-line, \\r\\n across writes), a keep-alive comment first."""
        text = (": keep-alive\r\nevent: %s\r\ndata: %s\r\n\r\n" % (name, data)).encode("utf-8")
        cuts = [3, len(text) // 2, len(text) - 1, len(text)]
        at = 0
        for c in cuts:
            self.wfile.write(text[at:c])
            self.wfile.flush()
            at = c
            time.sleep(0.002)

    # routing

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def route(self):
        u = urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        rec = {"method": self.command, "path": u.path, "query": dict(parse_qsl(u.query)), "body": body,
               "headers": {k.lower(): self.headers.get(k) for k in ("Authorization", "X-GaiaDesk-Desk-Token", "Content-Type", "Accept")}}
        self.server.requests.append(rec)
        try:
            self.handle_api(rec)
        except Exception as e:  # noqa: BLE001
            try:
                self.send_error_env({"kind": "protocol", "message": "mock API: %s" % e})
            except Exception:  # noqa: BLE001
                pass

    def handle_api(self, rec):
        auth = rec["headers"]["authorization"] or ""
        key = auth[7:] if auth.startswith("Bearer ") else ""
        if not key:
            return self.send_error_env({"kind": "refused", "reason": "unauthenticated", "message": "Sign in, or send an API key as `Authorization: Bearer ak_…`."})
        is_key = key.startswith("ak_")
        parts = rec["path"].split("/")
        if parts[:3] != ["", "v1", "desks"]:
            return self.send_error_env({"kind": "usage", "message": "no route %s %s" % (rec["method"], rec["path"])})
        if len(parts) == 3:
            return self.relay(run_fake(["devices", "--json"]), ok=(0, 1))
        desk = unquote(parts[3])
        rest = "/" + "/".join(parts[4:]) if len(parts) > 4 else ""
        if desk == HTML_DESK:
            body = b"<html><body>Internal Server Error</body></html>"
            self.send_response(500)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if desk == LIMITED_DESK:
            return self.send_error_env({"kind": "refused", "reason": "rate_limited", "message": "Too many requests for this key; try again in 7 s."},
                                       {"Retry-After": "7"})
        if rest == "" and rec["method"] == "GET":
            # One desk: these publish no end-to-end key (mock_e2e_api's do).
            return self.send_json(200, {"desk_id": desk, "online": True, "features": ["desk_op"]})
        tokens = rest.startswith("/tokens")
        if tokens and is_key:
            return self.send_error_env({"kind": "refused", "reason": "session_required", "desk": desk,
                                        "message": "token administration over the API works only for a signed-in person's own desk"})
        if not tokens and is_key and not rec["headers"]["x-gaiadesk-desk-token"]:
            return self.send_error_env({"kind": "refused", "reason": "desk_token_required", "desk": desk,
                                        "message": "from an API key, desk operations need a scoped agent token in X-GaiaDesk-Desk-Token"})
        q, m = rec["query"], rec["method"]

        def spec():
            return json.loads(rec["body"].decode("utf-8")) if rec["body"] else {}

        if rest == "/exec" and m == "POST":
            s = spec()
            stream = q.get("stream") == "1"
            args = ["exec", "--desk-id", desk, "--quiet", "--json-stream" if stream else "--json",
                    "--stdin" if isinstance(s.get("stdin"), str) else "--no-stdin"]
            if s.get("shell"):
                args += ["--shell", s["shell"]]
            if s.get("timeout_secs") is not None:
                args += ["--timeout", str(s["timeout_secs"])]
            if s.get("cwd"):
                args += ["--cwd", s["cwd"]]
            args += env_args(s.get("env"))
            args += ["--"] + (s["argv"] if isinstance(s.get("argv"), list) else [s["command"]])
            if stream:
                self.start_sse()
                state = {"ended": False}

                def on_line(line):
                    ev = parse(line)
                    if isinstance(ev, dict) and isinstance(ev.get("event"), str):
                        state["ended"] = state["ended"] or ev["event"] in ("exit", "error")
                        self.sse(ev["event"], json.dumps(ev))

                code, _, err = stream_fake(args, on_line, s.get("stdin") or "")
                if not state["ended"]:
                    self.sse("error", json.dumps({"event": "error", "exit": code, "error": plain_error(code, err)}))
                return
            code, out, err = run_fake(args, input=s.get("stdin") or "")
            r = parse(out)
            if isinstance(r, dict) and isinstance(r.get("exit"), int):
                if r.get("remote_code") is None and not r.get("timed_out") and isinstance(r.get("error"), dict):
                    error = dict(r["error"])
                    if r.get("desk") and not error.get("desk"):
                        error["desk"] = r["desk"]
                    return self.send_error_env(error)
                return self.send_json(200, r)
            return self.send_error_env(plain_error(code, err))

        if rest == "/jobs" and m == "POST":
            s = spec()
            args = ["run", "--detach", "--name", s["name"], "--desk-id", desk]
            if s.get("cwd"):
                args += ["--cwd", s["cwd"]]
            lim = s.get("limits") or {}
            if lim.get("priority"):
                args += ["--priority", lim["priority"]]
            if lim.get("cpu_percent") is not None:
                args += ["--cpu", str(lim["cpu_percent"])]
            if lim.get("mem_mb") is not None:
                args += ["--mem", str(lim["mem_mb"])]
            if lim.get("keep_awake") is True:
                args.append("--keep-awake")
            if lim.get("keep_awake") is False:
                args.append("--no-keep-awake")
            if s.get("shell"):
                args += ["--shell", s["shell"]]
            args += env_args(s.get("env"))
            return self.relay(run_fake(args + ["--json", "--"] + s["command"]), status=201)
        if rest == "/jobs" and m == "GET":
            return self.relay(run_fake(["ps", "--desk-id", desk, "--json"]))
        if rest.startswith("/jobs/"):
            sub = rest[len("/jobs/"):].split("/")
            name = unquote(sub[0])
            if len(sub) == 1 and m == "DELETE":
                return self.relay(run_fake(["kill", name, "--desk-id", desk, "--json"]))
            if sub[1:] == ["wait"] and m == "GET":
                # `wait --json`: the job (exit: its code; 124: the timeout ran out), as {job, timed_out}.
                WAITS.append(q.get("timeout"))
                args = ["wait", name, "--desk-id", desk, "--json"]
                if q.get("timeout") is not None and q.get("timeout") != "870":
                    args += ["--timeout", q["timeout"]]
                code, out, err = run_fake(args)
                j = parse(out)
                if not isinstance(j, dict) or isinstance(j.get("error"), dict):
                    return self.relay((code, out, err))
                result = {"job": j, "timed_out": code == 124 and j.get("state") == "running"}
                if name in ("held", "held-fail", "held-gone"):
                    # A held answer, as the API sends one: `GaiaDesk-Held: 1`, keep-alive spaces,
                    # then the result or (`held-fail`, `held-gone`) the error envelope in the 200,
                    # with `error.status` the status it would have had.
                    rid = request_id()
                    if name == "held-fail":
                        fail = {"error": {"kind": "connection_lost", "message": "The desk went away during this operation.",
                                          "reason": "desk_disconnected", "desk": desk, "request_id": rid, "status": 502}}
                    else:
                        fail = {"error": {"kind": "failed", "message": 'no job named "held-gone"', "desk": desk,
                                          "request_id": rid, "status": 422}}
                    payload = json.dumps(result if name == "held" else fail).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("X-Request-Id", rid)
                    self.send_header("GaiaDesk-Held", "1")
                    self.end_headers()
                    for _ in range(3):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                    self.wfile.write(payload)
                    self.close_connection = True
                    return None
                return self.send_json(200, result)
            if sub[1:] == ["logs"] and m == "GET":
                args = ["logs", name, "--desk-id", desk] + (["--follow"] if q.get("follow") == "1" else []) + ["--json"]
                if q.get("tail") is not None:
                    args += ["--tail", q["tail"]]
                if q.get("follow") != "1":
                    return self.relay(run_fake(args))
                self.start_sse()

                def on_log(line):
                    ev = parse(line)
                    if isinstance(ev, dict):
                        out = ev if isinstance(ev.get("event"), str) else {"event": "error", "error": ev.get("error")}
                        self.sse(out["event"], json.dumps(out))

                stream_fake(args, on_log)
                return
        if rest == "/stats" and m == "GET":
            return self.relay(run_fake(["stats", "--desk-id", desk, "--json"]))
        if rest == "/files":
            path = q.get("path")
            if not path:
                return self.send_error_env({"kind": "usage", "message": "path is required"})
            d = tempfile.mkdtemp(prefix="gaiadesk-mock-api-")
            name = [x for x in path.split("/") if x][-1] if path.strip("/") else "file"
            if m == "PUT":
                with open(os.path.join(d, name), "wb") as f:
                    f.write(rec["body"])
                # A summary with failed files is still the summary (the client makes it an error).
                return self.relay(run_fake(["cp", "--json", name, "%s:%s" % (desk, path)], cwd=d), ok=(0, 1))
            if m == "GET":
                ran = run_fake(["cp", "--json", "%s:%s" % (desk, path), name], cwd=d)
                if ran[0] != 0:
                    return self.relay(ran)
                body = ("contents of %s\n" % path).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
        if tokens:
            code = "session-owner"
            if rest == "/tokens" and m == "POST":
                s = spec()
                args = ["token", "create", "--desk", desk, "--name", s["name"], "--expires", "%ss" % s["expires_secs"], "--scope", ",".join(s["scopes"])]
                if s.get("cwd"):
                    args += ["--cwd", s["cwd"]]
                if s.get("low_priv"):
                    args.append("--low-priv")
                return self.relay(run_fake(args + ["--json"], code=code), status=201)
            if rest == "/tokens" and m == "GET":
                return self.relay(run_fake(["token", "list", "--desk", desk, "--json"], code=code))
            if rest.startswith("/tokens/") and m == "DELETE":
                return self.relay(run_fake(["token", "revoke", "--desk", desk, unquote(rest[len("/tokens/"):]), "--json"], code=code))
        self.send_error_env({"kind": "usage", "message": "no route %s %s" % (m, rec["path"])})


class MockApi:
    """``url`` (``http://127.0.0.1:<port>/v1``), ``requests`` (each one recorded), ``close()``."""

    def __init__(self, handler=Handler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.server.requests = []
        self.requests = self.server.requests
        self.url = "http://127.0.0.1:%d/v1" % self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def last(self):
        return self.requests[-1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
