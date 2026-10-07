"""A fake ``gaiadesk-cli`` for the tests: prints the JSON shapes the real CLI
documents, chosen by the desk id. Every run appends {argv, env, stdin} to
$FAKE_LOG so tests can check exactly what the SDK sent.

Desk ids: 100000001 fine; 100000002 offline (exec error kind); 100000003 the
desk refuses; 100000004 usage error; 100000005 a plain-stderr failure.

FAKE_TOOL_STYLE=underscore: the MCP server advertises ``gaiadesk_exec``
instead of ``gaiadesk.exec`` (and accepts only that spelling).
"""

import json
import os
import signal
import sys
import time

argv = sys.argv[1:]
OK, OFFLINE, REFUSED, USAGE, PLAIN = "100000001", "100000002", "100000003", "100000004", "100000005"


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


def job(name, **extra):
    j = {"name": name, "command": "make", "state": "running", "pid": 4242, "started_at_ms": 1700000000000, "log_bytes": 0, "by": "owner"}
    j.update(extra)
    return j


def exec_json(desk, cmd, stdin, exit=0):
    return {"exit": exit, "remote_code": exit, "stdout": "ran: %s%s\n" % (" ".join(cmd), "\nstdin: " + stdin if stdin else ""),
            "stderr": "warn\n", "duration_ms": 12, "desk": desk, "route": "LAN", "mode": "pipes", "shell": "/bin/zsh -l -c",
            "timed_out": False, "error": None, "notes": [], "truncated": False}


def fail_json(desk, kind, message, exit):
    return {"exit": exit, "remote_code": None, "stdout": "", "stderr": "", "duration_ms": 3, "desk": desk, "route": None, "mode": None,
            "shell": None, "timed_out": kind == "timeout", "error": {"kind": kind, "message": message}, "notes": [], "truncated": False}


def exec_like(desk, cmd, stdin, as_json):
    if desk == OFFLINE:
        if as_json:
            out(fail_json(desk, "offline", "desk %s is offline (last seen 4 min ago)" % desk, 255))
        else:
            err("gaiadesk-cli: desk %s is offline (last seen 4 min ago)" % desk)
        return 255
    if desk == USAGE:
        if as_json:
            out(fail_json(desk, "usage", "no credential: set GAIADESK_TOKEN_FILE or GAIADESK_CODE", 255))
        return 255
    if desk == PLAIN:
        err("gaiadesk-cli: something odd")
        return 255
    if desk == REFUSED:
        if as_json:
            r = exec_json(desk, cmd, "")
            r.update(exit=254, remote_code=-1, stdout="", stderr="", error="this agent token does not have the `exec` scope")
            out(r)
        return 254
    line = " ".join(cmd)
    code = int(line[5:]) if line.startswith("exit ") and line[5:].isdigit() else (124 if line == "sleep" else 0)
    if as_json:
        r = exec_json(desk, cmd, stdin, code)
        if line == "sleep":
            r["timed_out"] = True
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


UNDERSCORE = os.environ.get("FAKE_TOOL_STYLE") == "underscore"
EXEC_TOOL = "gaiadesk_exec" if UNDERSCORE else "gaiadesk.exec"
SHOT_TOOL = "gaiadesk_screenshot" if UNDERSCORE else "gaiadesk.screenshot"


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
    if cmd == "--version":
        out("gaiadesk-cli 0.1.0")
        return 0
    if cmd == "devices":
        rows = [
            {"desk_id": OK, "name": "office-pc", "online": True, "os": "windows", "app_version": "0.10.323", "owner": "you", "last_seen": 1700000000,
             "sources": ["account"], "last_ok": {"at": 1700000000, "route": "LAN"}, "last_failure": None, "reachable": None},
            {"desk_id": "999999999", "name": "nas", "online": False, "os": "linux", "app_version": None, "owner": "you", "last_seen": None,
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
        return exec_like(desk, after_dashes(), stdin if has("--stdin") else "", has("--json"))
    if cmd == "shell":
        return exec_like(desk, ["script:" + stdin.strip()], "", has("--json"))
    if cmd == "cp":
        pos = [a for a in argv[1:] if not a.startswith("-")]
        src, dst = pos
        remote = src if len(src) > 10 and src[:9].isdigit() and src[9] == ":" else dst
        d = remote.split(":")[0]
        if d == REFUSED:
            out({"refused": "file transfer is turned off for you"})
            return 254
        if d == PLAIN:
            err("gaiadesk-cli: desk 100000005 is offline (last seen 2 h ago)")
            return 255
        up = remote == dst
        failed = [{"path": "a.txt", "message": "permission denied"}] if "fail" in src or "fail" in dst else []
        out({"direction": "upload" if up else "download", "desk": d, "destination": remote[10:] if up else dst, "files": 2,
             "dirs": 1 if has("--recursive") else 0, "bytes": 2048, "resumed_bytes": 0, "failed": failed, "seconds": 0.5})
        return 1 if failed else 0
    if cmd == "run":
        if desk == REFUSED:
            out({"error": "this agent token does not have the `jobs` scope"})
            return 254
        out(job(flag("--name"), command=" ".join(after_dashes())))
        return 0
    if cmd == "ps":
        if desk == PLAIN:
            out("NAME  STATE")
            return 0
        out([job("build"), job("old", state="exited", exit_code=0)])
        return 0
    if cmd == "kill":
        if argv[1] == "nope":
            out({"error": "no job named nope"})
            err("gaiadesk-cli: no job named nope")
            return 1
        out(job(argv[1], state="killed"))
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
            out({"desk": desk, "error": "the desk did not answer"})
            err("gaiadesk-cli: the desk did not answer")
            return 255
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
            out({"error": "token administration needs the desk's unattended password"})
            return 254
        if sub == "create":
            desks = flag("--desk").split(",")
            f = flag("--out")
            if f:
                out({"tokens": [{"desk": d, "token": info} for d in desks], "file": f})
            else:
                out({"tokens": [{"desk": d, "token": info, "secret": "gdagt_%s_%s" % (d, "a" * 64)} for d in desks]})
            return 0
        if sub == "list":
            out([info])
            return 0
        if sub == "revoke":
            if has("--account"):
                out({"desk": desk, "ok": True, "message": "revoked through your account"})
                return 0
            rest = argv[2:]
            name = next((a for i, a in enumerate(rest) if not a.startswith("-") and (i == 0 or rest[i - 1] != "--desk")), None)
            if name == "ghost":
                out({"revoked": "", "stopped_sessions": 0})
                err("gaiadesk-cli: there was no live token on the desk to revoke")
                return 1
            out({"revoked": "bot" if has("--all-for-desk") else name, "stopped_sessions": 1})
            return 0
        return 255
    if cmd == "audit":
        out([{"at_ms": 5, "desk": desk, "token": "bot", "token_id": "9f3a", "action": "exec.end", "detail": "make test", "bytes": 0, "exit_code": 0, "duration_ms": 900}])
        return 0
    if cmd == "mesh":
        if argv[1] == "status":
            out({"self": {"desk_id": "111111111", "mesh_ip": "100.64.0.1"}, "peers": [{"desk_id": OK, "mesh_ip": "100.64.0.2", "online": True, "os": "windows"}]})
            return 0
        if argv[2] == OK:
            out("100.64.0.2")
            return 0
        err("gaiadesk-cli: desk %s is not on this machine's GaiaDesk Mesh" % argv[2])
        return 1
    if cmd == "disconnect":
        err("gaiadesk-cli: closed the held connection to desk %s" % (desk or "all"))
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
            err("gaiadesk-cli: an agent token is required (--token, or $GAIADESK_AGENT_TOKEN)")
            return 255
        out("agent session open on desk %s: screenshot 1280x800" % desk)
        return 0
    err("gaiadesk-cli: unknown subcommand %r" % cmd)
    return 255


if __name__ == "__main__":
    sys.exit(main())
