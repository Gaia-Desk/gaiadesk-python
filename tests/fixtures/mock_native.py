"""A stand-in for the ``gaiadesk_native`` extension (the native backend), for
the tests: the same fake desks as fake_cli.py, answering with the same JSON
shapes, failing with the extension's errors (``.kind``, ``.reason``, ``.json``).
Every call is recorded in ``calls``.

Results are the CLI's shapes (``job_list`` ``{jobs}``, ``job_logs``
``{job, output}``, ...); errors' ``.json`` is the error envelope.

Desks: 123456789 fine; 234567890 offline; 345678901 refuses;
desk-usage a usage error; desk-lost connection lost.
"""

import asyncio

OK, OFFLINE, REFUSED, USAGE, LOST = "123456789", "234567890", "345678901", "desk-usage", "desk-lost"


class NativeError(Exception):
    def __init__(self, message, kind, reason=None, json=None):
        super().__init__(message)
        self.kind = kind
        self.reason = reason
        self.json = json


def _error(message, kind, reason=None, desk=None):
    """The library's error: its ``.json`` is the CLI's envelope."""
    e = {"kind": kind, "message": message}
    if reason:
        e["reason"] = reason
    if desk:
        e["desk"] = desk
    return NativeError(message, kind, reason, {"error": e})


def _reach(desk):
    if desk == OFFLINE:
        raise _error("desk %s is offline (last seen 4 min ago)" % desk, "unreachable", "offline", desk)
    if desk == REFUSED:
        raise _error("this agent token does not have the `exec` scope", "refused", None, desk)
    if desk == USAGE:
        raise _error("no credential: set GAIADESK_TOKEN_FILE or GAIADESK_CODE", "usage", None, None)
    if desk == LOST:
        raise _error("the connection to desk %s was lost" % desk, "connection_lost", "connection_lost", desk)


def _exec_result(desk, line, stdin=None, cwd=None):
    exit = int(line[5:]) if line.startswith("exit ") and line[5:].isdigit() else (124 if line == "sleep" else 0)
    return {"exit": exit, "remote_code": exit, "stdout": "ran: %s\n%s%s" % (line, "stdin: %s\n" % stdin if stdin else "", "cwd: %s\n" % cwd if cwd else ""),
            "stderr": "warn\n", "duration_ms": 12, "desk": desk, "route": "LAN", "mode": "pipes", "shell": "/bin/zsh -l -c",
            "timed_out": line == "sleep", "error": None, "notes": [], "truncated": False}


class _Stream:
    def __init__(self, events, keep_open):
        self.events = list(events)
        self.keep_open = keep_open
        self.written = b""
        self.stopped = False

    def next_sync(self):
        if not self.events:
            return None
        return self.events.pop(0)

    async def next(self):
        await asyncio.sleep(0)
        return self.next_sync()

    def write(self, data):
        self.written += data

    def end(self):
        self.events = [("stdout", b"stdin: " + self.written), ("exit", {"exit": 0})]

    def stop(self):
        self.stopped = True
        self.events = [("exit", {"exit": 130, "error": {"kind": "failed", "message": "interrupted"}})]


class _Forward:
    def __init__(self, desk, specs):
        self.listening = [{"event": "listening", "local_port": s.get("local_port") or 54321, "desk": desk,
                           "remote_host": s.get("remote_host", "localhost"), "remote_port": s["remote_port"]} for s in specs]
        self.stopped = False

    def stop(self):
        self.stopped = True

    def wait_sync(self):
        return {"exit": 0, "error": None}

    async def wait(self):
        return self.wait_sync()


class _Screen:
    def call_sync(self, op, args=None):
        return ({"width": 1280, "height": 800}, b"png") if op == "screenshot" else ({"ok": True}, None)

    async def call(self, op, args=None):
        return self.call_sync(op, args)

    def close_sync(self):
        pass

    async def close(self):
        pass


def make_mock():
    """A fresh module-like object and its call log."""

    def listed(key, items):
        return {key: items}

    calls = []
    clients = []

    class NativeClient:
        backend = "mock"

        def __init__(self, options=None):
            options = options or {}
            if options.get("persist") == "forever":
                raise _error("options: persist: not a duration", "usage")
            clients.append(options)

        def call_sync(self, op, args=None, input=None):
            args = args or {}
            calls.append({"op": op, "args": args, "input": input})
            d = args.get("desk_id")
            if op == "version":
                return {"version": "0.10.324", "backend": "client"}
            if op == "devices":
                return {"devices": [{"desk_id": OK, "name": "office-pc", "online": True, "reachable": True if args.get("probe") else None}],
                        "sources": ["account"], "notes": []}
            if op in ("exec", "shell"):
                _reach(d)
                line = ("script:" + args["script"].strip()) if op == "shell" else (args["command"] if isinstance(args["command"], str) else " ".join(args["command"]))
                return _exec_result(d, line, input.decode() if input else None, args.get("cwd"))
            if op in ("upload", "download"):
                _reach(d)
                failed = [{"path": "a.txt", "message": "permission denied"}] if "fail" in args["local"] + args["remote"] else []
                s = {"direction": op, "desk": d, "destination": args["remote"] if op == "upload" else args["local"], "files": 2,
                     "dirs": 1 if args["recursive"] else 0, "bytes": 2048, "resumed_bytes": 0, "failed": failed, "seconds": 0.5}
                if failed:
                    raise NativeError("1 file failed to copy", "failed", None, s)
                return s
            if op == "job_run":
                _reach(d)
                return {"name": args["name"], "command": args["command"], "state": "running", "pid": 4242, "started_at_ms": 1, "log_bytes": 0, "by": "owner"}
            if op == "job_list":
                return listed("jobs", [{"name": "build", "command": "make", "state": "running", "started_at_ms": 1}])
            if op == "job_kill":
                if args["name"] == "nope":
                    raise _error("no job named nope", "failed", None, d)
                return {"name": args["name"], "command": "make", "state": "killed", "started_at_ms": 1, "log_bytes": 0, "by": "owner"}
            if op == "job_logs":
                output = "line 1\nline 2\n"
                return {"job": {"name": args["name"], "command": "make", "state": "running", "started_at_ms": 1}, "output": output}
            if op == "stats":
                _reach(d)
                return {"desk": d, "hostname": "office-pc"}
            if op == "measure":
                return {"desk": d, "sent": args.get("count", 10), "rtt_ms": None, "clock_offset_ms": None, "clock_uncertainty_ms": None}
            if op == "token_mint":
                return {"tokens": [{"desk": k, "token": {"label": args.get("name", "agent")}, "secret": "gdagt_x"} for k in args["desks"]]}
            if op == "token_list":
                return listed("tokens", [{"label": "bot", "id": "9f3a", "issued_at_ms": 1, "expires_at_ms": 2}])
            if op == "token_revoke":
                return {"desk": d, "ok": True, "message": "revoked"} if args["account"] else {"revoked": args.get("which", "all"), "stopped_sessions": 0}
            if op == "audit":
                return listed("events", [{"at_ms": 5, "desk": d, "token": "bot", "token_id": "9f3a", "action": "exec.end", "detail": "make"}])
            if op == "mesh_status":
                return {"self": None, "peers": []}
            if op == "mesh_ip":
                return {"desk_id": d, "mesh_ip": "100.64.0.1"}
            if op == "disconnect":
                return {"closed": [d] if d else []}
            raise _error("unknown op " + op, "usage")

        async def call(self, op, args=None, input=None):
            await asyncio.sleep(0)
            return self.call_sync(op, args, input)

        def stream_sync(self, op, args=None, input=None, keep_stdin_open=False):
            calls.append({"op": "stream:" + op, "args": args, "input": input, "keep_open": keep_stdin_open})
            _reach(args["desk_id"])
            if op == "job_follow":
                if args["name"] == "forever":
                    return _Stream([("stdout", b"line 1\n")] * 1000, False)
                return _Stream([("stdout", b"line 1\nline 2\n"), ("exit", {"exit": 0})], False)
            if keep_stdin_open:
                return _Stream([], True)
            line = "shell" if op == "shell" else (args["command"] if isinstance(args["command"], str) else " ".join(args["command"]))
            end = _exec_result(args["desk_id"], line, None, args.get("cwd"))
            for k in ("stdout", "stderr", "truncated"):
                del end[k]
            return _Stream([("stdout", ("part1 part2 %s\n" % line).encode()), ("stderr", b"warn\n"), ("exit", end)], False)

        async def stream(self, op, args=None, input=None, keep_stdin_open=False):
            return self.stream_sync(op, args, input, keep_stdin_open)

        def forward_sync(self, args):
            calls.append({"op": "forward", "args": args})
            _reach(args["desk_id"])
            return _Forward(args["desk_id"], args["specs"])

        async def forward(self, args):
            return self.forward_sync(args)

        def screen_sync(self, desk_id):
            calls.append({"op": "screen", "args": {"desk_id": desk_id}})
            _reach(desk_id)
            return _Screen()

        async def screen(self, desk_id):
            return self.screen_sync(desk_id)

    class Module:
        pass

    m = Module()
    m.NativeClient = NativeClient
    m.NativeError = NativeError
    m.build_info = lambda: {"version": "0.10.324", "backend": "client"}
    return m, calls, clients
