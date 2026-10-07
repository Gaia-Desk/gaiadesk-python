# GaiaDesk SDK for Python

Drive your GaiaDesk machines ("desks") from Python: list them and check
that they are reachable, run commands and get exit codes back, stream
output, copy files, run background jobs, read stats, mint and revoke scoped
agent tokens, forward ports, and reach the screen tools through MCP.

- Package: `gaiadesk` (Python 3.9+, sync and asyncio, typed: ships `py.typed`)
- Dependencies: none from Python 3.11 (`typing_extensions` before, for the
  types); one optional extra, `gaiadesk[native]`
- Works with any `gaiadesk-cli`; the newest features (`cwd`, streamed exec
  events) need 0.10.324 or later, which the SDK detects

**How it works.** Two backends, one API:

- **Native** (`pip install gaiadesk[native]`): GaiaDesk's client library as
  a prebuilt extension, `gaiadesk-native` (abi3 wheels for macOS, Linux
  x86_64/aarch64 glibc and Windows). Nothing else to install.
- **CLI** (otherwise): the SDK runs the `gaiadesk-cli` that ships with the
  GaiaDesk app and parses the JSON it prints with `--json`.

Both return the same results (the CLI's JSON shapes and field names) and
raise the same exceptions with the same `kind`s. This package contains no
GaiaDesk code; GaiaDesk itself is closed-source, and `gaiadesk-native` ships
under its own licence (see [Backends](#backends)). Where the CLI has no JSON
output, the SDK says so instead of guessing (see [Known gaps](#known-gaps)).

Other GaiaDesk developer tools:

- **TypeScript SDK**: [Gaia-Desk/gaiadesk-typescript](https://github.com/Gaia-Desk/gaiadesk-typescript) (`npm install @gaiadesk/sdk`)
- **MCP server** for AI assistants: [Gaia-Desk/gaiadesk-mcp](https://github.com/Gaia-Desk/gaiadesk-mcp) (`npx -y @gaiadesk/mcp`)
- **MCP or SDK?** [When to give a model the MCP server and when to use an SDK](https://github.com/Gaia-Desk/gaiadesk-mcp/blob/main/docs/mcp-vs-sdk.md)

MIT-licensed. GaiaDesk itself is proprietary and not covered by this license.

---

## Contents

- [Install](#install)
- [Backends](#backends)
- [Quickstart](#quickstart)
- [Credentials](#credentials)
- [API](#api)
- [Errors and exit codes](#errors-and-exit-codes)
- [Examples](#examples)
- [Known gaps](#known-gaps)
- [Development](#development)

---

## Install

```sh
pip install "gaiadesk[native]"
```

The `native` extra installs `gaiadesk-native`, and the SDK uses it: no
GaiaDesk app or CLI needed. With plain `pip install gaiadesk` (or on a
platform without a wheel) the SDK uses `gaiadesk-cli` instead: install
GaiaDesk (it includes the CLI) from <https://gaiadesk.net/download>. The SDK
finds `gaiadesk-cli` through `$GAIADESK_CLI`, then `PATH`, then the standard
locations (`/Applications/GaiaDesk.app/Contents/MacOS/gaiadesk-cli`,
`C:\Program Files\GaiaDesk\gaiadesk-cli.exe`, `/usr/bin/gaiadesk-cli`), or use
the `cli=` option to point at it.

## Backends

`gd.backend` says which one a client uses: `"native"` or `"cli"`.

| Option | Effect |
|---|---|
| `backend="auto"` (default) | native when `gaiadesk_native` imports, else the CLI. Passing `cli=` means the CLI. |
| `backend="native"` | native, or `CliNotFoundError` when it cannot load |
| `backend="cli"` | always `gaiadesk-cli` |
| `GAIADESK_SDK_BACKEND=auto\|native\|cli` | the default for `backend` |
| `native=module` | use this module instead of `import gaiadesk_native` |

`raw()` and `mcp()` always run `gaiadesk-cli` (they are the CLI's own
commands). On the native backend, `version()` returns `gaiadesk-native X.Y.Z`,
a stream's `argv` is the operation's name, `kill()` stops the remote side,
and an error's `exit_code` is the one `gaiadesk-cli` would have exited with.
The blocking client releases the GIL while it waits; cancelling an asyncio
task stops the native operation. `gaiadesk-native` is proprietary (free to use
with GaiaDesk; see its LICENSE); this SDK stays MIT.

## Quickstart

```python
from gaiadesk import GaiaDesk, RefusedError

gd = GaiaDesk(token_file="/home/me/.config/gaiadesk/bot.token")

for d in gd.devices()["devices"]:
    print(d["desk_id"], d["name"], d["online"])

r = gd.exec("123456789", "uname -a", shell="sh", timeout=60)
print(r["exit"], r["stdout"], r["route"])

try:
    gd.upload("./dist", "123456789", "deploy/", recursive=True)
except RefusedError as e:
    print("the token lacks the cp scope:", e)

# Stream output as it is produced:
s = gd.exec_stream("123456789", ["npm", "test"])
for stream, text in s.text():
    print(text, end="")
print("exit", s.wait().exit_code)
```

asyncio (`AsyncGaiaDesk` has the same methods as coroutines):

```python
import asyncio
from gaiadesk import AsyncGaiaDesk

async def main():
    gd = AsyncGaiaDesk(token_file="/home/me/.config/gaiadesk/bot.token")
    results = await asyncio.gather(*(gd.exec(d, "hostname") for d in ["123456789", "234567890"]))
    for r in results:
        print(r["desk"], r["stdout"].strip())

asyncio.run(main())
```

Results are plain dicts with the CLI's field names, typed as `TypedDict`s
generated from the CLI's own JSON Schema (`ExecResult`, `CopyResult`, `Job`,
`StatsReport`, `VersionInfo`, ... in
[`src/gaiadesk/types_generated.py`](src/gaiadesk/types_generated.py),
re-exported with the SDK's earlier names (`CpSummary`, `JobInfo`, ...) by
[`src/gaiadesk/types.py`](src/gaiadesk/types.py)).

## Credentials

Credentials are always passed to `gaiadesk-cli` through its environment,
never on its command line (other users on a machine can read command lines).
The native backend takes the same options and variables.
`gaiadesk-cli` never prompts when run by the SDK (there is no terminal), so a
missing credential fails fast with a `UsageError`.

| Option | Environment variable | Use |
|---|---|---|
| `token_file` | `GAIADESK_TOKEN_FILE` | **Recommended.** A scoped, expiring agent token file from `gaiadesk-cli token create --out <file>` (mode 0600). |
| `code` | `GAIADESK_CODE` | The desk's code or unattended password. Needed for token administration (`create_token`, `list_tokens`, `revoke_token`, `audit`), which only the desk's owner may do. An explicit `code` overrides an inherited `GAIADESK_TOKEN_FILE`. |
| `account_token` | `GAIADESK_TOKEN` | A GaiaDesk account session. Optional: by default the CLI uses its own sign-in (`gaiadesk-cli login`), which lists your account's desks and enables `--account` revokes and audits. |
| `agent_token` | `GAIADESK_AGENT_TOKEN` | An agent token with the `screen` scope, for `agent_connect` and the MCP screen tools. |
| `server` | `GAIADESK_SERVER` | Signaling server (`wss://…/ws`); default `wss://gaiadesk.net/ws`. Also passed as `--server` to `mcp` and `agent-connect`. |
| `persist` | `GAIADESK_PERSIST` | How long a desk connection is held for later commands (default `10m`; `0` = none). |
| `env` | | The base environment (default: this process's). |
| `cli` | `GAIADESK_CLI` | Path to `gaiadesk-cli`, or a command list. |
| `cwd` | | Working directory for `gaiadesk-cli` (relative local paths in copies resolve here). |

Mint, list and revoke tokens (the desk's owner, with the unattended password):

```python
import os
owner = GaiaDesk(code=os.environ["DESK_PASSWORD"])
owner.create_token("123456789", name="ci", scopes=["exec", "cp", "jobs"], expires="24h",
                   cwd="/srv/app", low_priv=True, out="/home/ci/.config/gaiadesk/ci.token")
owner.list_tokens("123456789")
owner.revoke_token("123456789", "ci")          # or all_for_desk=True; account=True via your signed-in account
owner.audit("123456789", token="ci", limit=100)
```

Scopes: `exec`, `shell`, `cp`, `forward`, `jobs`, `screen` (default
`exec,cp,jobs`). The desk enforces scopes, `cwd`, `low_priv` and expiry on
every request; the SDK does not.

## API

Every result is the CLI's JSON, with the CLI's field names (types in
[`src/gaiadesk/types.py`](src/gaiadesk/types.py)).

| Method | CLI | Returns |
|---|---|---|
| `version()` | `--version` | `"gaiadesk-cli X.Y.Z"` |
| `cli_version_info()` | `--version --json` | `{name, version, features[], json_shapes[], mcp_protocol_versions[]}`, or `None` from a CLI before 0.10.324 |
| `cli_features()` | `--version --json` | a frozenset (`exec_json_stream`, `exec_cwd`, `run_cwd`, ...); empty for an older CLI |
| `devices(probe=, desk_id=)` | `devices --json [--probe] [-d]` | `{devices[], sources[], notes[]}`; with `probe`, unreachable desks have `reachable: False` |
| `probe(desk_id)` | `devices --probe -d` | one device row with `probe` |
| `exec(desk_id, command, cwd=, **opts)` | `exec --json [--cwd]` | `{exit, remote_code, stdout, stderr, duration_ms, desk, route, mode, shell, timed_out, error, notes, truncated}` |
| `exec_stream(desk_id, command, cwd=, json_stream=, **opts)` | `exec --json-stream` (0.10.324+), else `exec` | a stream of stdout/stderr chunks, then the exit code (and, with events, `result`) |
| `shell(desk_id, script, **opts)` | `shell --json`, script on stdin | as `exec` |
| `shell_stream(desk_id, script=None, **opts)` | `shell` | stream; without a script, stdin stays open for `write()`/`end()` |
| `upload(local, desk_id, remote, recursive=)` | `cp --json <local> <desk>:<remote>` | `{direction, desk, destination, files, dirs, bytes, resumed_bytes, failed[], seconds}` |
| `download(desk_id, remote, local, recursive=)` | `cp --json <desk>:<remote> <local>` | as above |
| `run_job(desk_id, name, command, cwd=, priority=, cpu=, mem=, keep_awake=)` | `run --detach --json [--cwd]` | job `{name, command, state, pid, exit_code, started_at_ms, ended_at_ms, log_bytes, by, limits, enforcement}` |
| `jobs(desk_id)` | `ps --json` | job list (the CLI's `{"jobs": [...]}`, or an older CLI's bare array) |
| `job_logs(desk_id, name, tail=)` | `logs` | output text |
| `follow_job_logs(desk_id, name)` | `logs -f` | stream |
| `kill_job(desk_id, name)` | `kill --json` | job |
| `stats(desk_id)` | `stats --json` | `{desk, hostname, os, os_version, cpu_percent, cpus, load, mem_total_mb, mem_free_mb, disks[], uptime_secs, jobs_running}` |
| `measure(desk_id, count=)` | `measure --json` | `{desk, sent, rtt_ms{n,p50,p95,max}, clock_offset_ms, clock_uncertainty_ms}` |
| `create_token(desks, name=, expires=, scopes=, cwd=, low_priv=, out=)` | `token create --json` | `{tokens[{desk, token, secret?}], file?}` |
| `list_tokens(desk_id)` | `token list --json` | token list `{label, id, scopes, issued_at_ms, expires_at_ms, revoked, last_used_ms, cwd, low_priv}` (from `{"tokens": [...]}` or a bare array) |
| `revoke_token(desk_id, name, all_for_desk=, account=)` | `token revoke --json` | `{revoked, stopped_sessions}` or (account) `{desk, ok, message}` |
| `audit(desk_id, token=, limit=, account=)` | `audit --json` | event list `{at_ms, desk, token, token_id, action, detail, bytes, cwd, exit_code, duration_ms}` (from `{"events": [...]}` or a bare array) |
| `mesh_status()` | `mesh status --json` | `{self, peers[]}` |
| `mesh_ip(desk_id)` | `mesh ip` | the address |
| `disconnect(desk_id=None)` | `disconnect --desk-id \| --all` | nothing |
| `forward(desk_id, spec \| [spec])` | `forward --json` | a `Forward` with `listening`, `close()`, `wait()`; a context manager |
| `agent_connect(desk_id)` | `agent-connect` | the CLI's confirmation line |
| `mcp(audit_dir=, allow_domains=)` | `mcp` (stdio) | an MCP client: `list_tools()`, `call_tool(name, args)`, `close()`; a context manager |
| `raw(args, input=)` | anything | `Completed(code, stdout, stderr)`: the escape hatch |

`exec`/`shell` options: `shell` (`"default"` \| `"none"` \| `"sh"` \|
`"cmd"` \| `"pwsh"`), `timeout` (seconds or `"10m"`; `0` = none; CLI
default 30m), `connect_timeout` (default 60s), `persist`, `verbose`,
`stdin` (str or bytes; default closed; `exec_stream(stdin=True)` keeps it
open), `check` (raise `CommandError` on a non-zero exit), `cwd` (`exec`,
`exec_stream`, `run_job`: the directory it starts in on the desk; relative
paths are from the desk user's home, or from a confined token's folder).

**Feature detection.** The SDK asks `gaiadesk-cli --version --json` what it
can do, once per CLI (its path, size and modification time) per process,
and only when a call needs to know: a `cwd`, or `exec_stream`. A CLI from
before 0.10.324 does not understand that flag, which counts as "no
features". `cwd` with such a CLI raises `UsageError` (and nothing runs); it
is never silently dropped. The native backend takes `cwd` directly.

`command` as a **str** is one command line for the desk's shell, verbatim;
as a **list**, separate arguments that the desk quotes for its shell. The
default shell differs by desk: the user's login shell on macOS/Linux (zsh on
a Mac), `cmd.exe` on Windows. Pass `shell="sh"` for portable POSIX scripts
and `shell="pwsh"` for PowerShell.

Streams (`CliStream`, `AsyncCliStream`) yield `Chunk(stream, data)`;
`.text()` yields `(stream, text)` pairs; `.wait()` returns
`Exit(exit_code, stderr_tail)`; `.kill()` sends SIGINT, which
`gaiadesk-cli` turns into stopping the remote command (on Windows the
process is terminated instead).

With a CLI that has `exec --json-stream` (0.10.324+), `exec_stream` returns
a `JsonExecStream` (`AsyncJsonExecStream`) built from its events: the same
chunks, plus `.result` once it has ended: the last event,
`{"event": "exit", exit, remote_code, duration_ms, desk, route, mode, shell,
timed_out, error, notes}`, or `{"event": "error", "exit", "error": {kind,
message, reason}}` when the command never ran (then `Exit.stderr_tail` is
that message). The output arrives as text (bytes that are not UTF-8 are
replaced); pass `json_stream=False` for the plain `exec` byte stream
(`result` is then `None`), or `json_stream=True` to require the events.

### Screen tools (via MCP)

The CLI exposes the screen (Agent Access: screenshots, clicks, typing) only
through `gaiadesk-cli mcp`. The SDK's `mcp()` starts it and speaks its
protocol (MCP 2026-07-28, stateless):

```python
import os
from gaiadesk import GaiaDesk, tool_image

gd = GaiaDesk(agent_token=os.environ["GAIADESK_AGENT_TOKEN"])
with gd.mcp(audit_dir="/var/log/gaiadesk-agent") as m:
    opened = m.call_tool("gaiadesk_open_session", {"desk_id": "123456789"})
    session = opened["structuredContent"]["session_id"]
    shot = tool_image(m.call_tool("gaiadesk_screenshot", {"session_id": session}))
    m.call_tool("gaiadesk_click", {"session_id": session, "x": 200, "y": 140})
    m.call_tool("gaiadesk_close_session", {"session_id": session})
```

`AsyncGaiaDesk.mcp()` returns an `AsyncMcpClient` (`async with await
gd.mcp() as m`) that matches concurrent requests to replies. Every tool and
its arguments are listed in the
[gaiadesk-mcp README](https://github.com/Gaia-Desk/gaiadesk-mcp#the-tools)
("The tools"), and `list_tools()` returns their schemas. Tool names are
`gaiadesk_<tool>` from `gaiadesk-cli` 0.10.324 (`gaiadesk.<tool>` before);
`call_tool` accepts either spelling and sends the one the server
advertises, so use `gaiadesk_exec`, `gaiadesk_screenshot`, ... with any CLI.
The client speaks the stateless MCP revision (2026-07-28), which every CLI's
server takes; 0.10.324+ also speaks the standard `initialize` lifecycle.

## Errors and exit codes

| Error | When |
|---|---|
| `CliNotFoundError` | `gaiadesk-cli` could not be started |
| `UsageError` | kind `usage`: bad arguments (from the SDK, or the CLI, including "no credential"); also `cwd` with a CLI before 0.10.324 |
| `RefusedError` | kind `refused`, exit 254: wrong code, token without the scope, expired or revoked, permission off, a `cwd` outside a confined token's folder |
| `UnreachableError` | kind `unreachable`: `offline`, `unknown_desk`, `not_online`, `network`, `not_signed_in`, `timeout` |
| `ConnectionLostError` | kind `connection_lost`, or `shell` exit 253 |
| `OperationFailedError` | kind `failed` / exit 1 from a desk operation: a file failed (the summary is in `.json`), no such job, nothing to revoke |
| `ProtocolError` | kind `protocol` (the desk is too old for the request), or the CLI printed something other than its documented JSON |
| `CommandError` | `exec`/`shell` with `check=True` and a non-zero exit (`.result` has the output) |
| `McpError` | a JSON-RPC error from `gaiadesk-cli mcp` (`.code`) |
| `GaiaDeskError` | the base class; also exit 255 from a desk operation (`kind == "cli_error"`) |

The native backend raises the same exceptions with the same kinds (an
unreachable desk is `UnreachableError` with `kind == "offline"`,
`"unknown_desk"`, ...; `"unreachable"` only when the library gives no finer
reason). `CliNotFoundError` there means `backend="native"` was asked for and
`gaiadesk_native` could not load.

Every error carries `exit_code`, `kind`, `reason`, `desk`, `stderr`, `argv`
and the parsed `json` when there was one.

From 0.10.324 every `--json` failure is one envelope,
`{"error": {"kind", "message", "reason"?, "desk"?}}`, with `kind` one of
`usage`, `refused`, `unreachable`, `connection_lost`, `failed`, `protocol`
(the class above) and `reason` the finer cause. An error's `kind` is that
reason when it is one the SDK knows (`offline`, `timeout`, `local`, ...),
else the envelope's kind; `reason` and `desk` are the envelope's. Older
CLIs' shapes (`{"error": "<text>"}`, `{"refused": "<text>"}`, exec's
fine-grained kinds, a sentence on stderr) are still read, all in one place
(`error_envelope` in [`src/gaiadesk/errors.py`](src/gaiadesk/errors.py)).

A non-zero exit from **your command** is not an error: `exec` returns it in
`exit` (and `remote_code`), with `timed_out: True` and exit 124 when
`--timeout` stopped it. Exit codes from the CLI:

| Code | `exec` / `shell` | desk operations |
|---|---|---|
| 0-255 | the remote command's own | 0 done, 1 did not succeed |
| 124 | `--timeout` ran out | |
| 130 | interrupted (SIGINT) | |
| 253 | `shell`: connection lost / desk ended the terminal | |
| 254 | the desk refused | the desk refused |
| 255 | the CLI's own error (offline, unreachable, bad arguments) | the CLI's own error |

## Examples

[`examples/`](examples):

| File | What |
|---|---|
| `exec_on_a_desk.py` | find a reachable desk, run a command, handle the outcomes |
| `copy_a_file.py` | upload, run, download, resume |
| `run_a_job.py` | a background job with caps; poll it, read its log, stop it |

When should a model drive the desk instead of your code? See
[MCP or SDK?](https://github.com/Gaia-Desk/gaiadesk-mcp/blob/main/docs/mcp-vs-sdk.md).

## Known gaps

Things the CLI does not (yet) offer, so neither does the SDK (the TypeScript SDK has the same list):

1. **Older CLIs.** Before `gaiadesk-cli` 0.10.324 there is no `--cwd` (the
   SDK raises `UsageError`), no `exec --json-stream` (streams are plain
   bytes with no `result`), no feature list, and errors outside
   `exec`/`shell` have no kind: the SDK raises `RefusedError` (254),
   `OperationFailedError` (1) or `GaiaDeskError` with `kind == "cli_error"`
   (255) with the CLI's sentence. Before that release `run --detach` also
   re-quoted a single command line; pass `run_job` an argument list there.
2. **No JSON for `job_logs` and `mesh_ip` here yet.** The CLI has
   `logs --json` and `mesh ip --json` from 0.10.324; the SDK still reads
   their text (the same with every CLI). `forward --json` prints only
   `listening` events.
3. **`token create --out --json`** prints `{tokens: [{desk, token}], file}`,
   a shape the CLI's schema does not list (typed by hand here as
   `TokenCreateResult`).
4. **No interactive terminal.** `shell` is interactive only on a real TTY;
   the SDK runs it over pipes (a script, or lines you write). `shell` has no
   `--cwd`.
5. **Durations** are whole seconds or `30s`/`10m`/`2h`-style strings;
   fractional seconds are rounded up.
6. **Not wrapped:** `gaiadesk-cli login`/`logout` (interactive device flow;
   run it once, or pass `account_token`), `agent run` (the bring-your-own-key
   screen agent, which needs a model API key and writes reports),
   `support` and `provision` (app/installer plumbing). Use `raw()`.

## Development

```sh
python -m pip install -e .        # only typing_extensions, before Python 3.11
python -m unittest discover -s tests -v
```

The tests put `src/` on `sys.path` and never touch a real desk.
[`tests/fixtures/fake_cli.py`](tests/fixtures/fake_cli.py) prints the JSON
shapes the real CLI documents and records the argv and environment it was
given; it plays a CLI of 0.10.324 or later, and (through
`fake_cli_old.py`) one from before, so every change is tested against both.
[`tests/fixtures/mock_native.py`](tests/fixtures/mock_native.py) does the
same for the native library.

`src/gaiadesk/types_generated.py` is generated by GaiaDesk's
`scripts/gen-sdk-types.mts` from the CLI's schema
(`gaiadesk-cli schema --json`); do not edit it. CI runs on Linux, macOS and Windows with Python
3.9 to 3.13 ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## License

MIT. See [LICENSE](LICENSE). GaiaDesk itself is proprietary software and is
not covered by this license.
