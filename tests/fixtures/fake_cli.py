"""A fake ``gaiadesk-cli`` for the tests: prints the JSON shapes the real CLI
documents, chosen by the desk id. Every run appends {argv, env, stdin} to
$FAKE_LOG so tests can check exactly what the SDK sent.

Two CLIs in one: by default a CLI of 0.10.324 or later (``--version --json``
with features, one error envelope, ``{"jobs": [...]}``-style lists,
``exec --json-stream``, ``--cwd`` (exec, run, shell), ``--json`` on logs /
mesh ip / disconnect / agent-connect, ``gaiadesk_*`` MCP tool names). With
FAKE_CLI=old (or through fake_cli_old.py) a CLI from before it: text
``--version``, ``{"error": "<text>"}`` / ``{"refused": "<text>"}``, exec's
fine-grained error kinds, bare arrays, no ``--cwd`` / ``--json-stream`` (a
usage error), text from logs / mesh ip / disconnect / agent-connect, dotted
MCP tool names.

Desks: 123456789 fine; 234567890 offline; 345678901 the desk refuses;
desk-usage a usage error; desk-plain a failure told only on stderr.
"""

import json
import os
import signal
import sys
import time

argv = sys.argv[1:]
OK, OFFLINE, REFUSED, USAGE, PLAIN = "123456789", "234567890", "345678901", "desk-usage", "desk-plain"
DESKS = (OK, OFFLINE, REFUSED, USAGE, PLAIN)
OLD = os.environ.get("FAKE_CLI") == "old"
FEATURES = ["json_error_envelope", "json_v1_shapes", "exec_json_stream", "exec_cwd", "run_cwd", "shell_cwd", "run_verbatim_command", "logs_json",
            "mesh_ip_json", "disconnect_json", "agent_connect_json", "login_json", "mcp_lifecycle", "mcp_underscore_tool_names",
            "mcp_structured_content"]


def out(v):
    sys.stdout.write((v if isinstance(v, str) else json.dumps(v)) + "\n")
    sys.stdout.flush()


def err(s):
    sys.stderr.write(s + "\n")
    sys.stderr.flush()


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None


def has(name):
    return name in argv


def after_dashes():
    return argv[argv.index("--") + 1:] if "--" in argv else []


def log(stdin):
    path = os.environ.get("FAKE_LOG")
    if not path:
        return
    env = {k: os.environ[k] for k in ("GAIADESK_TOKEN_FILE", "GAIADESK_CODE", "GAIADESK_TOKEN", "GAIADESK_AGENT_TOKEN", "GAIADESK_SERVER", "GAIADESK_PERSIST") if k in os.environ}
    with open(path, "a") as f:
        f.write(json.dumps({"argv": argv, "env": env, "stdin": stdin}) + "\n")


def failure(kind, message, exit, old_text=None, reason=None, desk=None, old_key="error"):
    """A desk operation's failure: the envelope (new), or the older CLI's own shape."""
    if OLD:
        out({old_key: old_text or message})
    else:
        e = {"kind": kind, "message": message}
        if reason:
            e["reason"] = reason
        if desk:
            e["desk"] = desk
        out({"error": e})
    err("gaiadesk-cli: " + message)
    return exit


def listed(key, items):
    """``ps`` / ``token list`` / ``audit``: an object from 0.10.324, a bare array before."""
    out(items if OLD else {key: items})
    return 0


def job(name, **extra):
    j = {"name": name, "command": "make", "state": "running", "pid": 4242, "started_at_ms": 1700000000000, "log_bytes": 0, "by": "owner"}
    j.update(extra)
    return j


def ran_text(cmd, stdin, cwd):
    return "ran: %s%s%s\n" % (" ".join(cmd), "\nstdin: " + stdin if stdin else "", "\ncwd: " + cwd if cwd else "")


def exec_json(desk, cmd, stdin, exit=0, cwd=None):
    return {"exit": exit, "remote_code": exit, "stdout": ran_text(cmd, stdin, cwd),
            "stderr": "warn\n", "duration_ms": 12, "desk": desk, "route": "LAN", "mode": "pipes", "shell": "/bin/zsh -l -c",
            "timed_out": False, "error": None, "notes": [], "truncated": False}


def exec_error(kind, reason):
    """exec's ``error`` for a run that never started: the six kinds + reason, or (old) the fine kind."""
    if OLD:
        return {"kind": reason or kind}
    e = {"kind": kind}
    if reason:
        e["reason"] = reason
    return e


def fail_json(desk, error, message, exit):
    error = dict(error, message=message)
    return {"exit": exit, "remote_code": None, "stdout": "", "stderr": "", "duration_ms": 3, "desk": desk, "route": None, "mode": None,
            "shell": None, "timed_out": False, "error": error, "notes": [], "truncated": False}


def exec_like(desk, cmd, stdin, mode, cwd=None):
    """``mode``: "json" (one object), "stream" (``--json-stream`` events) or "plain"."""

    def not_run(error, message, exit):
        if mode == "json":
            out(fail_json(desk, error, message, exit))
        elif mode == "stream":
            out({"event": "error", "exit": exit, "error": dict(error, message=message)})
        err("gaiadesk-cli: " + message)
        return exit

    if desk == OFFLINE:
        return not_run(exec_error("unreachable", "offline"), "desk %s is offline (last seen 4 min ago)" % desk, 255)
    if desk == USAGE:
        return not_run(exec_error("usage", None), "no credential: set GAIADESK_TOKEN_FILE or GAIADESK_CODE", 255)
    if desk == PLAIN:
        err("gaiadesk-cli: something odd")
        return 255
    if desk == REFUSED:
        message = "this agent token does not have the `exec` scope"
        if OLD:
            if mode == "json":
                r = exec_json(desk, cmd, "")
                r.update(exit=254, remote_code=-1, stdout="", stderr="", error=message)
                out(r)
            return 254
        return not_run({"kind": "refused", "desk": desk}, message, 254)
    line = " ".join(cmd)
    code = int(line[5:]) if line.startswith("exit ") and line[5:].isdigit() else (124 if line == "sleep" else 0)
    if mode == "json":
        r = exec_json(desk, cmd, stdin, code, cwd)
        if line == "sleep":
            r["timed_out"] = True
            r["error"] = None if OLD else {"kind": "failed", "message": "the command ran past --timeout and was stopped"}
        out(r)
        return code
    if mode == "stream":
        out({"event": "stdout", "data": "part1 "})
        time.sleep(0.03)
        out({"event": "stdout", "data": "part2 %s\n" % line})
        if stdin:
            out({"event": "stdout", "data": "stdin: %s\n" % stdin})
        if cwd:
            out({"event": "stdout", "data": "cwd: %s\n" % cwd})
        out({"event": "stderr", "data": "warn\n"})
        r = exec_json(desk, cmd, stdin, code, cwd)
        for k in ("stdout", "stderr", "truncated"):
            del r[k]
        r["event"] = "exit"
        out(r)
        return code
    sys.stdout.write("part1 ")
    sys.stdout.flush()
    time.sleep(0.03)
    sys.stdout.write("part2 %s\n" % line)
    if stdin:
        sys.stdout.write("stdin: %s\n" % stdin)
    sys.stdout.flush()
    err("warn")
    return code


EXEC_TOOL = "gaiadesk.exec" if OLD else "gaiadesk_exec"
SHOT_TOOL = "gaiadesk.screenshot" if OLD else "gaiadesk_screenshot"


def mcp():
    for raw in sys.stdin:
        if not raw.strip():
            continue
        m = json.loads(raw)
        if "id" not in m:
            continue
        meta = (m.get("params") or {}).get("_meta") or {}
        if meta.get("io.modelcontextprotocol/protocolVersion") != "2026-07-28" or "io.modelcontextprotocol/clientCapabilities" not in meta:
            out({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32602, "message": "_meta is required on every request"}})
            continue
        p = m["params"]
        if m["method"] == "tools/list":
            out({"jsonrpc": "2.0", "id": m["id"], "result": {"resultType": "complete", "argv": argv,
                 "tools": [{"name": EXEC_TOOL, "inputSchema": {"type": "object"}}, {"name": SHOT_TOOL, "inputSchema": {"type": "object"}}]}})
        elif m["method"] == "tools/call" and p["name"] == EXEC_TOOL:
            s = exec_json(p["arguments"]["desk_id"], [p["arguments"]["command"]], "")
            out({"jsonrpc": "2.0", "id": m["id"], "result": {"resultType": "complete", "content": [{"type": "text", "text": "exit 0"}], "structuredContent": s, "isError": False}})
        elif m["method"] == "tools/call" and p["name"] == SHOT_TOOL:
            out({"jsonrpc": "2.0", "id": m["id"], "result": {"resultType": "complete", "content": [{"type": "image", "data": "iVBORw0K", "mimeType": "image/png"}], "isError": False}})
        elif m["method"] == "tools/call":
            out({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32602, "message": "Unknown tool: %s" % p["name"]}})
        else:
            out({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "Method not found: %s" % m["method"]}})
    return 0


def main():
    cmd = argv[0] if argv else ""
    if cmd == "mcp":
        log("")
        return mcp()
    stdin = sys.stdin.read()
    log(stdin)
    desk = flag("--desk-id") or flag("--desk")
    text_only = cmd in ("logs", "disconnect", "agent-connect") or (cmd == "mesh" and argv[1:2] == ["ip"])
    if OLD and (has("--cwd") and cmd in ("exec", "run", "shell") or has("--json-stream") or (text_only and has("--json"))):
        err("gaiadesk-cli: unknown option %s" % next(f for f in ("--cwd", "--json-stream", "--json") if has(f)))
        return 255
    if cmd == "--version":
        if has("--json") and not OLD:
            out({"name": "gaiadesk-cli", "version": "0.10.324", "features": FEATURES, "json_shapes": ["v1", "v2"],
                 "mcp_protocol_versions": ["2026-07-28", "2025-11-25"]})
        else:
            out("gaiadesk-cli %s" % ("0.10.323" if OLD else "0.10.324"))
        return 0
    if cmd == "devices":
        rows = [
            {"desk_id": OK, "name": "office-pc", "online": True, "os": "windows", "app_version": "0.10.323", "owner": "you", "last_seen": 1700000000,
             "sources": ["account"], "last_ok": {"at": 1700000000, "route": "LAN"}, "last_failure": None, "reachable": None},
            {"desk_id": OFFLINE, "name": "nas", "online": False, "os": "linux", "app_version": None, "owner": "you", "last_seen": None,
             "sources": ["mesh"], "last_ok": None, "last_failure": {"at": 1, "kind": "no_route", "message": "not found"}, "reachable": None},
        ]
        rows = [r for r in rows if not desk or r["desk_id"] == desk]
        if has("--probe"):
            for r in rows:
                r["reachable"] = r["desk_id"] == OK
                r["probe"] = {"ok": True, "dialled": True, "route": "LAN", "rtt_ms": 4} if r["desk_id"] == OK else {"ok": False, "dialled": True, "kind": "no_route"}
        out({"devices": rows, "sources": ["account", "mesh"], "notes": []})
        return 1 if any(r["reachable"] is False for r in rows) else 0
    if cmd == "exec":
        mode = "stream" if has("--json-stream") else ("json" if has("--json") else "plain")
        return exec_like(desk, after_dashes(), stdin if has("--stdin") else "", mode, flag("--cwd"))
    if cmd == "shell":
        return exec_like(desk, ["script:" + stdin.strip()], "", "json" if has("--json") else "plain", flag("--cwd"))
    if cmd == "cp":
        pos = [a for a in argv[1:] if not a.startswith("-")]
        src, dst = pos
        remote = src if src.split(":")[0] in DESKS else dst
        d, path = remote.split(":", 1)
        if d == REFUSED:
            return failure("refused", "file transfer is turned off for you", 254, old_key="refused", desk=d)
        if d == PLAIN:
            err("gaiadesk-cli: desk %s is offline (last seen 2 h ago)" % d)
            return 255
        up = remote == dst
        failed = [{"path": "a.txt", "message": "permission denied"}] if "fail" in src or "fail" in dst else []
        out({"direction": "upload" if up else "download", "desk": d, "destination": path if up else dst, "files": 2,
             "dirs": 1 if has("--recursive") else 0, "bytes": 2048, "resumed_bytes": 0, "failed": failed, "seconds": 0.5})
        return 1 if failed else 0
    if cmd == "run":
        if desk == REFUSED:
            return failure("refused", "this agent token does not have the `jobs` scope", 254, desk=desk)
        out(job(flag("--name"), command=" ".join(after_dashes())))
        return 0
    if cmd == "ps":
        if desk == PLAIN:
            out("NAME  STATE")
            return 0
        return listed("jobs", [job("build"), job("old", state="exited", exit_code=0)])
    if cmd == "kill":
        if argv[1] == "nope":
            return failure("failed", "no job named nope", 1, desk=desk)
        out(job(argv[1], state="killed"))
        return 0
    if cmd == "logs" and has("--json"):
        if argv[1] == "nope":
            return failure("failed", "no job named nope", 1, desk=desk)
        if has("--follow"):
            for l in ("one", "two", "three"):
                out({"event": "output", "data": l + "\n"})
                time.sleep(0.02)
            if argv[1] == "lost":
                return failure("connection_lost", "the connection to the desk was lost", 255, desk=desk)
            out({"event": "end", "job": job(argv[1], state="exited", exit_code=0)})
            err("job %s exited (exit 0)" % argv[1])
            return 0
        out({"job": job(argv[1]), "output": "tail\n" if flag("--tail") else "line1\nline2\n"})
        return 0
    if cmd == "logs":
        if has("--follow"):
            for l in ("one", "two", "three"):
                out(l)
                time.sleep(0.02)
            err("job %s exited (exit 0)" % argv[1])
            return 0
        if argv[1] == "nope":
            err("gaiadesk-cli: no job named nope")
            return 1
        sys.stdout.write("tail\n" if flag("--tail") else "line1\nline2\n")
        return 0
    if cmd == "stats":
        if desk == PLAIN:
            if OLD:
                out({"desk": desk, "error": "the desk did not answer"})
                err("gaiadesk-cli: the desk did not answer")
                return 255
            return failure("unreachable", "the desk did not answer", 255, reason="timeout", desk=desk)
        out({"desk": desk, "hostname": "office-pc", "os": "windows", "os_version": "Windows 11 Pro", "cpu_percent": 37.5, "cpus": 8, "load": None,
             "mem_total_mb": 16384, "mem_free_mb": 4096, "disks": [{"mount": "C:\\", "total_mb": 512000, "free_mb": 64000}], "uptime_secs": 3600, "jobs_running": 2})
        return 0
    if cmd == "measure":
        good = desk == OK
        out({"desk": desk, "sent": int(flag("--count") or 20), "rtt_ms": {"n": 20, "p50": 4, "p95": 9, "max": 12} if good else None,
             "clock_offset_ms": 1.5 if good else None, "clock_uncertainty_ms": 0.5 if good else None})
        return 0 if good else 1
    if cmd == "token":
        sub = argv[1]
        info = {"label": "bot", "id": "9f3a1c2b7d004e11", "scopes": ["exec", "cp"], "issued_at_ms": 1, "expires_at_ms": 2, "revoked": False}
        if not os.environ.get("GAIADESK_CODE"):
            return failure("refused", "token administration needs the desk's unattended password", 254)
        if sub == "create":
            desks = flag("--desk").split(",")
            f = flag("--out")
            if f:
                out({"tokens": [{"desk": d, "token": info} for d in desks], "file": f})
            else:
                out({"tokens": [{"desk": d, "token": info, "secret": "gdagt_%s_%s" % (d, "a" * 64)} for d in desks]})
            return 0
        if sub == "list":
            return listed("tokens", [info])
        if sub == "revoke":
            if has("--account"):
                out({"desk": desk, "ok": True, "message": "revoked through your account"})
                return 0
            rest = argv[2:]
            name = next((a for i, a in enumerate(rest) if not a.startswith("-") and (i == 0 or rest[i - 1] != "--desk")), None)
            if name == "ghost":
                if OLD:
                    out({"revoked": "", "stopped_sessions": 0})
                    err("gaiadesk-cli: there was no live token on the desk to revoke")
                    return 1
                return failure("failed", "there was no live token on the desk to revoke", 1, desk=desk)
            out({"revoked": "bot" if has("--all-for-desk") else name, "stopped_sessions": 1})
            return 0
        return 255
    if cmd == "audit":
        return listed("events", [{"at_ms": 5, "desk": desk, "token": "bot", "token_id": "9f3a", "action": "exec.end", "detail": "make test",
                                  "bytes": 0, "exit_code": 0, "duration_ms": 900}])
    if cmd == "mesh":
        if argv[1] == "status":
            out({"self": {"desk_id": REFUSED, "mesh_ip": "100.64.0.1"}, "peers": [{"desk_id": OK, "mesh_ip": "100.64.0.2", "online": True, "os": "windows"}]})
            return 0
        if argv[2] == OK:
            out({"desk_id": OK, "mesh_ip": "100.64.0.2", "renamed_to": None} if has("--json") else "100.64.0.2")
            return 0
        if has("--json"):
            return failure("failed", "desk %s is not on this machine's GaiaDesk Mesh" % argv[2], 1, desk=argv[2])
        err("gaiadesk-cli: desk %s is not on this machine's GaiaDesk Mesh" % argv[2])
        return 1
    if cmd == "disconnect":
        closed = [desk or OK]
        for d in closed:
            err("gaiadesk-cli: closed the held connection to desk %s" % d)
        if has("--json"):
            out({"closed": closed})
        return 0
    if cmd == "forward":
        pairs = [a for a in argv[1:] if a != "--json"]
        d = pairs[0].split(":")[0]
        if d == REFUSED:
            err("gaiadesk-cli: the desk refused the forward: no `forward` scope")
            return 254
        for i in range(0, len(pairs), 2):
            parts = pairs[i].split(":")
            lp = int(pairs[i + 1].split(":")[1]) or 40000 + i
            out({"event": "listening", "local_port": lp, "desk": d, "remote_host": parts[1] if len(parts) == 3 else "127.0.0.1", "remote_port": int(parts[-1])})
        stop = {"v": False}

        def on_sig(*_):
            stop["v"] = True

        signal.signal(signal.SIGINT, on_sig)
        signal.signal(signal.SIGTERM, on_sig)
        deadline = time.time() + 10
        while not stop["v"] and time.time() < deadline:
            time.sleep(0.02)
        return 0
    if cmd == "agent-connect":
        if not os.environ.get("GAIADESK_AGENT_TOKEN"):
            if has("--json"):
                return failure("usage", "an agent token is required (--token, or $GAIADESK_AGENT_TOKEN)", 255)
            err("gaiadesk-cli: an agent token is required (--token, or $GAIADESK_AGENT_TOKEN)")
            return 255
        if desk == REFUSED and has("--json"):
            return failure("refused", "the desk refused the agent session: no `screen` scope", 254, desk=desk)
        if has("--json"):
            out({"desk_id": desk, "ok": True, "screenshot": {"width": 1280, "height": 800}})
        else:
            out("agent session open on desk %s: screenshot 1280x800" % desk)
        return 0
    err("gaiadesk-cli: unknown subcommand %r" % cmd)
    return 255


if __name__ == "__main__":
    sys.exit(main())
