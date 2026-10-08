# GaiaDesk SDK for Python

Drive your GaiaDesk machines ("desks") from Python: list them and check
that they are reachable, run commands and get exit codes back, stream
output, copy files, run background jobs, read stats, mint and revoke scoped
agent tokens, forward ports, and reach the screen tools through MCP.

- Package: `gaiadesk` (Python 3.9+, sync and asyncio, typed: ships `py.typed`)
- Dependencies: none from Python 3.11 (`typing_extensions` before, for the
  types); one optional extra, `gaiadesk[native]`
- Reads the JSON the current `gaiadesk-cli` prints (one error envelope,
  object-shaped lists, `exec --json-stream`)

**How it works.** Two backends, one API:

- **Native** (`pip install gaiadesk[native]`): GaiaDesk's client library as
  a prebuilt extension, `gaiadesk-native` (abi3 wheels for macOS, Linux
  x86_64/aarch64 glibc and Windows). Nothing else to install.
- **CLI** (otherwise): the SDK runs the `gaiadesk-cli` that ships with the
  GaiaDesk app and parses the JSON it prints with `--json`.

Both return the same results (the CLI's JSON shapes and field names) and
raise the same exceptions with the same `kind`s. A third transport, **API**
(give an `api_key`), drives desks through GaiaDesk's hosted HTTPS API with
the standard library only: see [API transport](#api-transport). This package contains no
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
- [API transport](#api-transport)
  - [End-to-end encryption](#end-to-end-encryption)
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
platform without a wheel) the SDK uses `gaiadesk-cli` instead. Install the
CLI on its own (no GaiaDesk app needed) with one of:

```sh
curl -fsSL https://raw.githubusercontent.com/Gaia-Desk/gaiadesk-cli/main/install.sh | sh   # macOS, Linux
brew install gaia-desk/tap/gaiadesk                                                         # Homebrew
npm install -g @gaiadesk/cli                                                                # any OS with Node.js
```

(PowerShell: `irm https://raw.githubusercontent.com/Gaia-Desk/gaiadesk-cli/main/install.ps1 | iex`;
direct downloads and checksums: [Gaia-Desk/gaiadesk-cli](https://github.com/Gaia-Desk/gaiadesk-cli).)
The GaiaDesk app (<https://gaiadesk.net/download>) includes it too. The SDK
finds `gaiadesk-cli` through `$GAIADESK_CLI`, then `PATH`, then the standard
locations (`/Applications/GaiaDesk.app/Contents/MacOS/gaiadesk-cli`,
`C:\Program Files\GaiaDesk\gaiadesk-cli.exe`, `/usr/bin/gaiadesk-cli`), or use
the `cli=` option to point at it.

## Backends

`gd.backend` says which one a client uses: `"native"` or `"cli"` (or
`"api"`, given an `api_key`: see [API transport](#api-transport)).

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

## API transport

Given an `api_key`, the client talks to GaiaDesk's hosted API
(`https://api.gaiadesk.net/v1`) over HTTPS with the standard library only
(`http.client`): no `gaiadesk-cli`, no native extension. Without `api_key`,
nothing changes: the client picks the native or CLI backend exactly as
before.

```python
import os
from gaiadesk import GaiaDesk, AsyncGaiaDesk

gd = GaiaDesk(
    api_key=os.environ["GAIADESK_API_KEY"],        # an API key (ak_…), or a signed-in person's session token
    desk_token=os.environ["GAIADESK_DESK_TOKEN"],  # a scoped agent token (gdagt_…), verified by the desk
    # base_url="https://api.gaiadesk.net/v1",      # the default
    # wake=60,                                     # ring a sleeping desk and wait up to 60 s (wake_s)
)
gd.backend  # "api"
r = gd.exec("123456789", "hostname")
```

`AsyncGaiaDesk(api_key=...)` is the same for asyncio (the HTTP calls run on
the default executor's threads).

**Credentials.** Every request carries `Authorization: Bearer <api_key>`
and, when set, `X-GaiaDesk-Desk-Token: <desk_token>`. From an API key, desk
operations need a scoped agent token (`gdagt_…`, minted with
`create_token` or `gaiadesk-cli token create`) in `desk_token`: the API
relays it to the desk, which verifies it. A signed-in person's own session
works on their own desks without one. **Token administration**
(`create_token`, `list_tokens`, `revoke_token`) over the API works only for
a signed-in person's own desk, never from an API key. `wake=<0-120>` rings
a sleeping desk before each operation and waits that many seconds.

What it serves, with the same results and errors as the CLI transport:

| Method | API |
|---|---|
| `devices(desk_id=)` | `GET /desks` (`{devices, sources, notes}`; `desk_id` filters it) |
| `exec(desk_id, command, ...)` | `POST /desks/{id}/exec` (an `ExecSpec`: `command` or `argv`, `shell`, `cwd`, `stdin`, `timeout_secs`) |
| `exec_stream(desk_id, command, ...)` | `POST /desks/{id}/exec?stream=1` (Server-Sent Events of `ExecEvent`s) |
| `run_job`, `jobs`, `kill_job` | `POST` / `GET /desks/{id}/jobs`, `DELETE /desks/{id}/jobs/{name}` |
| `job_logs(desk_id, name, tail=)` | `GET /desks/{id}/jobs/{name}/logs?tail=` |
| `follow_job_logs(desk_id, name)` | `GET …/logs?follow=1` (Server-Sent Events of `JobLogEvent`s) |
| `wait_job(desk_id, name, timeout=)` | `GET /desks/{id}/jobs/{name}/wait?timeout=` (`{job, timed_out}`; one request holds at most 870 s, so a longer or no `timeout` asks again until the job ends) |
| `stats(desk_id)` | `GET /desks/{id}/stats` |
| `upload(local, desk_id, remote)` | `PUT /desks/{id}/files?path=` with the file's bytes, streamed (a `remote` ending in `/` keeps the file name) |
| `download(desk_id, remote, local)` | `GET /desks/{id}/files?path=` into `local` (a folder keeps the remote name) |
| `upload_bytes(data, desk_id, remote)` / `download_bytes(desk_id, remote)` | the same with bytes in memory (API transport only) |
| `create_token(desks, name=, expires=, scopes=, cwd=, low_priv=)` | `POST /desks/{id}/tokens` (a `MintSpec`), once per desk |
| `list_tokens(desk_id)` / `revoke_token(desk_id, id)` | `GET /desks/{id}/tokens` / `DELETE /desks/{id}/tokens/{token_id}` |

- **Files** are single files of at most **256 MB** each way; copy folders
  and larger files through the CLI or native transport.
- **Streaming**: `exec_stream` and `follow_job_logs` return a stream with
  the same shape as on the CLI (chunks, `text()`, `wait()` for the `Exit`,
  `result` for the last event); `kill()` closes the request. stdin is given
  up front (`stdin=` text or bytes); `stdin=True` is not available.
- **Errors** are the same classes and kinds, from the API's error envelope
  (`{"error": {kind, message, reason?, desk?, request_id}}`); the exception
  also has `status` (HTTP), `request_id` (quote it to support) and
  `retry_after` (seconds, on a 429). No connection is `UnreachableError`
  with kind `network`; an answer that is not the envelope is a
  `ProtocolError`.
- `env=` and `shell=` go in the `ExecSpec` / `JobSpec` (`shell="powershell"` is
  sent as `pwsh`, as gaiadesk-cli maps it). Values are never logged by the
  API or the desk; a low-privilege token's desk refuses them, as for the CLI.
- `timeout=` becomes `timeout_secs`; `connect_timeout`, `persist` and
  `verbose` do not apply. `create_token` needs a `name` over the API (and
  defaults `expires` to 7 days, `scopes` to exec, cp, jobs).

**Not available over the API** (a `UsageError`, kind `usage`, saying "not
available over the API transport; use the CLI or native transport"):
`shell`, `shell_stream`, `forward`, `agent_connect`, `mcp`, `measure`,
`mesh_status`, `mesh_ip`, `disconnect`, `audit`, `whoami`,
`probe` / `devices(probe=True)`, recursive copies, `create_token(out=)`,
`revoke_token(all_for_desk=True)` / `account=True`, `exec_stream` with
`stdin=True` or `json_stream=False`, and the CLI's own `version`,
`cli_version_info`, `cli_features`, `raw`.

### End-to-end encryption

```sh
pip install "gaiadesk[e2e]"    # adds cryptography (Apache-2.0 / BSD)
```

With the `e2e` extra installed, desk operations over the API are **sealed**
so the server relays only ciphertext: it never sees the command, its
environment, stdin, a file's name or bytes, or any output. Nothing changes in
your code: every method returns and raises exactly what it does in the clear.

Before an operation the SDK reads the desk's key (`GET /desks/{id}`: its
`e2e_pub` and `e2e_required`, cached for 5 minutes), makes a fresh X25519
key pair for that one operation, derives three keys with HKDF-SHA256 and
seals the request, an upload's bytes and every answer with
XChaCha20-Poly1305 bound to the desk, the operation and each frame's place
in its stream (GaiaDesk's design and test vectors: `protocol/src/e2e.rs`,
`protocol/src/e2e/vectors.json`, both reproduced by this SDK's tests). The
server still sees the API key and agent token, the route (operation and
desk; job names and token ids in a path), `stream`/`follow`/`wake_s`, sizes
and how the operation ended.

```python
gd = GaiaDesk(
    api_key=..., desk_token=...,
    e2e="auto",                    # the default; or "require", or "off"
    e2e_keys={"123456789": "B6N8vBQgk8i3VdwbEOhstCY3StFqqFPtC9_AsrhtHHw"},  # optional: pin a desk's e2e_pub
)
```

- **`e2e="auto"`** (default): sealed whenever the desk publishes a key. A desk
  with no key (offline, or a GaiaDesk from before this), or a missing
  `cryptography` package, is sent in the clear with a `RuntimeWarning`
  (once) — unless the desk requires encryption (`e2e_required`, its
  **Settings → Agent access → "Require end-to-end encryption for API
  commands"**): then the SDK wakes it and reads its key again, and raises
  `EndToEndError` (naming `pip install "gaiadesk[e2e]"` when that is what is
  missing) rather than send plaintext.
- **`e2e="require"`**: never in the clear; no key (after a wake) or no
  `cryptography` is an `EndToEndError` (reason `e2e_unavailable`) and nothing
  is sent.
- **`e2e="off"`**: in the clear, as before (no key lookup).
- **`e2e_keys`** pins keys: a different key handed out by the server is an
  `EndToEndError` (reason `e2e_key_mismatch`) and nothing is sent. A pinned
  key is used even when the server lists none.
- **Retries.** A plaintext operation the desk refuses `409 e2e_required` is
  sent again sealed, once (an upload only when its source can be read
  again). A sealed one refused `e2e_decrypt_failed` (the desk's key changed)
  is sealed again to the key read anew, once.
- **Answers that do not open** (altered, reordered, replayed, or sent in the
  clear in place of a sealed answer) are an `EndToEndError` (reason
  `e2e_decrypt_failed` / `e2e_malformed`); in a stream, its `result` is a
  `protocol` error. A desk's own errors are opened so their messages are the
  desk's.

The `local` and `lan` transports talk to the desk itself and never seal.

## Local and LAN

GaiaDesk desks serve the same `/v1` desk operations themselves, so the API
transport's methods, results, errors and streams work against a desk
directly (standard library only, no hosted API in between):

```python
import os
from gaiadesk import GaiaDesk

# Code running ON the desk: its own GaiaDesk over a Unix socket
# ($GAIADESK_API_DIR/api.sock, else ~/.gaiadesk/api.sock) or, on Windows,
# the named pipe \\.\pipe\gaiadesk-api-<user> ($GAIADESK_API_PIPE).
gd = GaiaDesk(transport="local")
gd.backend  # "local"
r = gd.exec("123456789", "hostname")

# A desk's opt-in LAN gateway, its self-signed certificate pinned by the
# SHA-256 fingerprint the desk's Settings shows (with or without colons).
lan = GaiaDesk(
    transport="lan",
    base_url="https://gaiadesk-123456789.local:7443/v1",
    fingerprint="ab:cd:…",                  # 32 hex pairs
    desk_token=os.environ["GAIADESK_DESK_TOKEN"],  # required: agent tokens only on the LAN
)
lan.devices()  # the desk, plus paired desks it reaches on its LAN (ops on them are forwarded)
```

- **`local`** needs Settings → GaiaDesk API → Local API on. Credentials:
  `desk_token=` (an agent token, sent as `X-GaiaDesk-Desk-Token`), else the
  desk's local admin token: `token=`, else the `api-token` file beside the
  socket (`$GAIADESK_API_DIR/api-token`, else `~/.gaiadesk/api-token`; on
  Windows `%USERPROFILE%\.gaiadesk\api-token`), sent as
  `Authorization: Bearer`. `socket_path=` gives another socket path or pipe
  name. No socket (or no token file) is `UnreachableError` with reason
  `local_api_unavailable`.
- **`lan`** requires `https://`, a `fingerprint` and a `desk_token`. The
  certificate is checked right after the TLS handshake, before a byte of the
  request is sent; another certificate is `FingerprintMismatchError` (an
  `UnreachableError`, reason `fingerprint_mismatch`).
- Both serve what the API transport serves (same table above, same
  256 MB file limit and `UsageError` for the rest); `wake` does not apply.
  `AsyncGaiaDesk(transport=...)` is the same for asyncio.
- Helpers: `normalize_fingerprint`, `certificate_fingerprint`,
  `local_api_dir`, `local_socket_path`, `local_token_path`,
  `local_pipe_name`, `pipe_user`, `default_local_address`.

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
| `cli_version_info()` | `--version --json` | `{name, version, features[], mcp_protocol_versions[]}`, or `None` from a CLI too old to answer it |
| `cli_features()` | `--version --json` | a frozenset (`exec_json_stream`, `exec_cwd`, `run_cwd`, ...) |
| `whoami()` | `whoami --json` | `{source, account}`: `source` is `app`, `login`, `token` or `none` (not signed in: a result, not an error) |
| `devices(probe=, desk_id=)` | `devices --json [--probe] [-d]` | `{devices[], sources[], notes[], identity}`; with `probe`, unreachable desks have `reachable: False` |
| `probe(desk_id)` | `devices --probe -d` | one device row with `probe` |
| `exec(desk_id, command, cwd=, env=, **opts)` | `exec --json [--cwd] [--env KEY]...` | `{exit, remote_code, stdout, stderr, duration_ms, desk, route, mode, shell, timed_out, error, notes, truncated}` |
| `exec_stream(desk_id, command, cwd=, env=, json_stream=, **opts)` | `exec --json-stream` (`json_stream=False`: `exec`) | a stream of stdout/stderr chunks, then the exit code (and, with events, `result`) |
| `shell(desk_id, script, **opts)` | `shell --json [--cwd]`, script on stdin | as `exec` |
| `shell_stream(desk_id, script=None, **opts)` | `shell [--cwd]` | stream; without a script, stdin stays open for `write()`/`end()` |
| `upload(local, desk_id, remote, recursive=)` | `cp --json <local> <desk>:<remote>` | `{direction, desk, destination, files, dirs, bytes, resumed_bytes, failed[], seconds}` |
| `download(desk_id, remote, local, recursive=)` | `cp --json <desk>:<remote> <local>` | as above |
| `run_job(desk_id, name, command, cwd=, shell=, env=, priority=, cpu=, mem=, keep_awake=)` | `run --detach --json [--cwd] [--shell] [--env KEY]...` | job `{name, command, state, pid, exit_code, started_at_ms, ended_at_ms, log_bytes, by, limits, enforcement, reason}` |
| `wait_job(desk_id, name, timeout=)` | `wait <job> --json [--timeout]` | `{job, timed_out}`: the job as it ended (its `exit_code` is a result, not an error), or, `timed_out`, as it stands, still running |
| `jobs(desk_id)` | `ps --json` | job list (from `{"jobs": [...]}`) |
| `job_logs(desk_id, name, tail=)` | `logs --json` | output text (the `output` of `{job, output}`) |
| `follow_job_logs(desk_id, name)` | `logs -f --json` | stream; `.result` is the `end` / `interrupted` / `error` event |
| `kill_job(desk_id, name)` | `kill --json` | job |
| `stats(desk_id)` | `stats --json` | `{desk, hostname, os, os_version, cpu_percent, cpus, load, mem_total_mb, mem_free_mb, disks[], uptime_secs, jobs_running}` |
| `measure(desk_id, count=)` | `measure --json` | `{desk, sent, rtt_ms{n,p50,p95,max}, clock_offset_ms, clock_uncertainty_ms}` |
| `create_token(desks, name=, expires=, scopes=, cwd=, low_priv=, out=)` | `token create --json` | `MintResult` `{tokens[{desk, token, secret}]}`; with `out`, `TokenFileResult` `{tokens[{desk, token}], file}` |
| `list_tokens(desk_id)` | `token list --json` | token list `{label, id, scopes, issued_at_ms, expires_at_ms, revoked, last_used_ms, cwd, low_priv}` (from `{"tokens": [...]}`) |
| `revoke_token(desk_id, name, all_for_desk=, account=)` | `token revoke --json` | `{revoked, stopped_sessions}` or (account) `{desk, ok, message}` |
| `audit(desk_id, token=, limit=, account=)` | `audit --json` | event list `{at_ms, desk, token, token_id, action, detail, bytes, cwd, exit_code, duration_ms}` (from `{"events": [...]}`) |
| `mesh_status()` | `mesh status --json` | `{self, peers[]}` |
| `mesh_ip(desk_id)` | `mesh ip --json` | the address |
| `disconnect(desk_id=None)` | `disconnect --desk-id \| --all [--json]` | `{closed[]}` |
| `forward(desk_id, spec \| [spec])` | `forward --json` | a `Forward` with `listening`, `close()`, `wait()`; a context manager |
| `agent_connect(desk_id)` | `agent-connect --json` | the confirmation line ("agent session open on desk N: screenshot WxH") |
| `mcp(audit_dir=, allow_domains=)` | `mcp` (stdio) | an MCP client: `list_tools()`, `call_tool(name, args)`, `close()`; a context manager |
| `raw(args, input=)` | anything | `Completed(code, stdout, stderr)`: the escape hatch |

`exec`/`shell` options: `shell` (`"default"` \| `"none"` \| `"sh"` \|
`"bash"` \| `"zsh"` \| `"cmd"` \| `"pwsh"` \| `"powershell"` (sent as `pwsh`); `run_job` takes the same but
`default`/`none`), `timeout` (seconds or `"10m"`; `0` = none; CLI
default 30m), `connect_timeout` (default 60s), `persist`, `verbose`,
`stdin` (str or bytes; default closed; `exec_stream(stdin=True)` keeps it
open), `check` (raise `CommandError` on a non-zero exit), `cwd` (`exec`,
`exec_stream`, `run_job`: the directory it starts in on the desk; relative
paths are from the desk user's home, or from a confined token's folder),
`env` (`exec`, `exec_stream`, `run_job`: `{NAME: value}`, environment
variables for the command, never logged by the desk; through gaiadesk-cli
each is a bare `--env KEY` and its value goes in that gaiadesk-cli
process's own environment, so values never appear on this machine's
command lines and keep newlines. The exception: a name that would change how
gaiadesk-cli itself runs, `GAIADESK_*` (any case), `PATH`, `HOME`,
`USERPROFILE`, `TMPDIR`, `TEMP`, `TMP`, `LANG`, `LC_ALL`, `SystemRoot`,
`ComSpec` (any case on Windows), goes as `--env KEY=VALUE` on its command
line instead. The native backend passes them in-process). A program Windows Smart App Control / WDAC
refused to start is `error["reason"] == "blocked_by_os_policy"` in an exec
result, and a job's `reason` (its `state` then says so in words).

**Feature detection.** The SDK asks `gaiadesk-cli --version --json` what it
can do, once per CLI (its path, size and modification time) per process,
and only when a call needs to know: a `cwd`. A CLI without the `exec_cwd`,
`run_cwd` or `shell_cwd` feature (or too old to answer `--version --json`)
raises `UsageError` saying to update gaiadesk-cli, and nothing runs; `cwd` is
never silently dropped. The native backend takes `cwd` directly.

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

`exec_stream` runs `exec --json-stream` and returns a `JsonExecStream` (`AsyncJsonExecStream`) built from its events: the same
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
`gaiadesk_<tool>` (`gaiadesk_exec`, `gaiadesk_screenshot`, ...). The client
speaks the stateless MCP revision (2026-07-28), which the server takes beside
the standard `initialize` lifecycle.

## Errors and exit codes

| Error | When |
|---|---|
| `CliNotFoundError` | `gaiadesk-cli` could not be started |
| `UsageError` | kind `usage`: bad arguments (from the SDK, or the CLI, including "no credential"); also `cwd` with a CLI that lacks the feature (update gaiadesk-cli) |
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

Every `--json` failure is one envelope,
`{"error": {"kind", "message", "reason"?, "desk"?}}`, with `kind` one of
`usage`, `refused`, `unreachable`, `connection_lost`, `failed`, `protocol`
(the class above) and `reason` the finer cause. An error's `kind` is that
reason when it is one the SDK knows (`offline`, `timeout`, `local`, ...),
else the envelope's kind; `reason` and `desk` are the envelope's. It is
read in one place (`error_envelope` in
[`src/gaiadesk/errors.py`](src/gaiadesk/errors.py)).

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

1. **`forward --json`** prints only `listening` events.
2. **`exec --json` buffers** the whole output (up to 16 MB per stream); use
   `exec_stream` for large output.
3. **No interactive terminal.** `shell` is interactive only on a real TTY;
   the SDK runs it over pipes (a script, or lines you write).
4. **Durations** are whole seconds or `30s`/`10m`/`2h`-style strings;
   fractional seconds are rounded up.
5. **Not wrapped:** `gaiadesk-cli login`/`logout` (interactive device flow;
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
given (through `fake_cli_old.py` it plays a CLI too old to answer
`--version --json`, for the `cwd` checks).
[`tests/fixtures/mock_native.py`](tests/fixtures/mock_native.py) does the
same for the native library, and
[`tests/fixtures/mock_api.py`](tests/fixtures/mock_api.py) for the hosted
API (an `http.server` that answers every route from the same fake CLI);
[`tests/fixtures/mock_e2e_api.py`](tests/fixtures/mock_e2e_api.py) is that API
and a desk in one for sealed operations. The end-to-end tests need
`cryptography` (`pip install -e ".[e2e]"`) and are skipped without it.
[`tests/test_transports.py`](tests/test_transports.py) runs the same
behavioural cases against the CLI and the API transport.

`src/gaiadesk/types_generated.py` is generated by GaiaDesk's
`scripts/gen-sdk-types.mts` from the CLI's schema
(`gaiadesk-cli schema --json`); do not edit it. CI runs on Linux, macOS and Windows with Python
3.9 to 3.13 ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## License

MIT. See [LICENSE](LICENSE). GaiaDesk itself is proprietary software and is
not covered by this license.
