# Changelog

## 0.1.0 (unreleased)

The API transport's `env=`, `shell=` and `wait_job`:

- `env=` on `exec`, `exec_stream` and `run_job`, `shell=` on `run_job`, and
  `wait_job` (`GET /desks/{id}/jobs/{name}/wait`; a `timeout` past the API's
  870-second hold, or none, asks again until the job ends; a held answer's
  keep-alive spaces and in-body error envelope are read) over the API.
- `powershell` is a shell name everywhere `pwsh` is, sent as `pwsh` (as
  gaiadesk-cli maps it). Regenerated types: `Shell` has `powershell`.

For `gaiadesk-cli` / `gaiadesk-native` 0.10.324:

- **API transport.** `GaiaDesk(api_key=..., desk_token=None, base_url=None,
  wake=None)` (and `AsyncGaiaDesk`) drives desks through GaiaDesk's hosted
  API (`https://api.gaiadesk.net/v1`) with the standard library only: no
  gaiadesk-cli, no native extension. Same method names, result shapes and
  exception classes/kinds for what the API serves (`devices`, `exec`,
  `exec_stream` over SSE, `run_job`, `jobs`, `kill_job`, `job_logs`,
  `follow_job_logs` over SSE, `stats`, single-file `upload` / `download` up
  to 256 MB, `create_token`, `list_tokens`, `revoke_token`), plus
  `upload_bytes` / `download_bytes`; everything else is a `UsageError`
  saying it is not available over the API transport. Exceptions gain
  `status`, `request_id` and `retry_after` (None on the other backends).
  `backend` is `"api"` for such a client. Constructing without `api_key`
  behaves exactly as before.
- `wait_job(desk_id, name, timeout=)` (`wait <job> --json`, the native
  `job_wait`): blocks until the job ends; `{job, timed_out}`. A job's own
  non-zero exit code is a result, not an error.
- `env=` (`{NAME: value}`) on `exec`, `exec_stream` and `run_job`
  (a bare `--env KEY`, the value in gaiadesk-cli's own environment, never on
  its command line, newlines kept; `--env KEY=VALUE` only for names that
  would change how the CLI itself runs: `GAIADESK_*`, `PATH`, `HOME`, ...;
  the native library's `env`); `shell=` on `run_job`
  (`sh`, `bash`, `zsh`, `cmd`, `pwsh`); `bash` and `zsh` for `exec`/`shell`.
- `whoami()` (`whoami --json`, the native `whoami`): `{source, account}`;
  not signed in (`source: "none"`) is a result, not an error.
  `devices()` has `identity`.
- Regenerated types: `Identity`, `JobWaitResult`, `Job.reason`
  (`blocked_by_os_policy`: Windows Smart App Control / WDAC), `env` and
  `shell` in the run shapes, `bash`/`zsh` in `Shell`.
- Types generated from the CLI's JSON Schema (`types_generated.py`, by
  GaiaDesk's `scripts/gen-sdk-types.mts`): `ExecResult`, `ExecExit`,
  `ExecEvent`, `Error`, `ErrorEnvelope`, `Job`, `JobList`, `TokenList`,
  `AuditLog`, `VersionInfo`, ... re-exported from `gaiadesk.types`; the
  earlier names are aliases (`JobInfo` = `Job`, `CpSummary` = `CopyResult`,
  `DevicesResult` = `DeviceList`, ...). `stats()` is typed `StatsReport`.
  New dependency before Python 3.11: `typing_extensions` (only
  `gaiadesk.types` needs it).
- The CLI's one error envelope, `{"error": {kind, message, reason?, desk?}}`
  with the six kinds (`usage`, `refused`, `unreachable`, `connection_lost`,
  `failed`, `protocol`), read by `error_envelope`; errors gain `.reason` and
  `.desk`. `kind` stays the finer cause when there is one (`offline`, ...).
- `cwd=` on `exec`, `exec_stream` and `run_job` (`--cwd`; the native
  library's `cwd`). With a CLI without the feature it raises `UsageError`
  (update gaiadesk-cli) instead of running the command elsewhere.
- `exec_stream` uses `exec --json-stream`: the same
  chunks, plus `.result` (the `exit` or `error` event: route, shell, an
  error's kind). `json_stream=False` keeps the plain byte stream.
- Feature detection: `cli_version_info()` / `cli_features()`
  (`--version --json`, cached per CLI).
- `job_logs`, `follow_job_logs`, `mesh_ip`, `disconnect` and
  `agent_connect` use the `--json` forms, so their failures are typed by
  the error envelope. `disconnect()` returns `{"closed": [...]}` (both
  backends); `follow_job_logs` has `.result` (`end`, `interrupted` or
  `error`).
- `cwd=` on `shell` and `shell_stream` (`shell --cwd`, feature `shell_cwd`;
  a CLI without it raises `UsageError`).
- `create_token` is typed by the generated `MintResult` / `MintFileResult`
  (`TokenFileResult`, the `out=` form); `TokenCreateResult` is their union
  instead of a hand-written dict.
- `jobs()`, `list_tokens()`, `audit()` read `{"jobs": [...]}` /
  `{"tokens": [...]}` / `{"events": [...]}`; `job_logs()` and `mesh_ip()`
  read `{job, output}` / `{mesh_ip}`.
- MCP: `gaiadesk_<tool>` names; `call_tool` sends the name as given.
- A `check=True` `CommandError` now has `kind == "failed"` on both backends.
- `gaiadesk[native]` needs `gaiadesk-native>=0.10.324`.
- Tests use placeholder desk ids only.

- A native backend: with `gaiadesk-native` installed (`pip install
  gaiadesk[native]`: GaiaDesk's client library as a prebuilt extension),
  every method runs on it instead of spawning `gaiadesk-cli`, sync and
  asyncio. Same results, same exceptions and kinds. `backend=` /
  `GAIADESK_SDK_BACKEND` (`auto` | `native` | `cli`), `gd.backend`, `native=`
  to inject a module. `raw()` and `mcp()` stay on the CLI.

First version of `gaiadesk` (Python 3.9+, sync and asyncio), over
`gaiadesk-cli`:

- `devices` / `probe`, `exec` / `exec_stream`, `shell` / `shell_stream`,
  `upload` / `download`, `run_job` / `jobs` / `job_logs` / `follow_job_logs` /
  `kill_job`, `stats`, `measure`, `create_token` / `list_tokens` /
  `revoke_token`, `audit`, `mesh_status` / `mesh_ip`, `disconnect`, `forward`,
  `agent_connect`, `mcp()` (a client for `gaiadesk-cli mcp`, MCP 2026-07-28),
  and `raw()`. `AsyncGaiaDesk` has the same methods as coroutines.
- Typed errors mapped from the CLI's exit codes and `--json` error kinds.
  Every error envelope the CLI prints is read in one place
  (`error_envelope`).
- Credentials passed only through the environment, never argv.
- Locates `gaiadesk-cli` via `$GAIADESK_CLI`, `PATH`, then the standard
  install locations.
- Tests against a fake `gaiadesk-cli`; CI on Linux, macOS and Windows with
  Python 3.9 to 3.13.
- Split out of `Gaia-Desk/gaiadesk-sdk` (now
  [Gaia-Desk/gaiadesk-typescript](https://github.com/Gaia-Desk/gaiadesk-typescript)).
