# Changelog

## 0.1.0 (unreleased)

For `gaiadesk-cli` / `gaiadesk-native` 0.10.324:

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
