# Changelog

## 0.1.0 (unreleased)

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
- `McpClient.call_tool` / `AsyncMcpClient.call_tool` accept tool names with
  a dot or an underscore (`gaiadesk.exec` / `gaiadesk_exec`) and send the
  spelling the server advertises.
- Credentials passed only through the environment, never argv.
- Locates `gaiadesk-cli` via `$GAIADESK_CLI`, `PATH`, then the standard
  install locations.
- Tests against a fake `gaiadesk-cli`; CI on Linux, macOS and Windows with
  Python 3.9 to 3.13.
- Split out of `Gaia-Desk/gaiadesk-sdk` (now
  [Gaia-Desk/gaiadesk-typescript](https://github.com/Gaia-Desk/gaiadesk-typescript)).
