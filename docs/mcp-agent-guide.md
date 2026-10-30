# DOSBox-X debugging over MCP

The Windows adapter owns one DOSBox-X process and one active DOS target session. It serves Streamable HTTP at `http://127.0.0.1:8765/mcp`, using the official MCP Python SDK. The guest display remains available. Debugger control belongs to the agent during a managed session.

## Build and install

Use Windows x64, Visual Studio 2022 with the Desktop development with C++ workload, the Windows SDK, and the toolsets required by the existing solution. Run these commands from the repository root in a Visual Studio Developer PowerShell:

```powershell
msbuild .\vs\dosbox-x.sln /m /t:dosbox-x '/p:Configuration=Agent Debug SDL2' /p:Platform=x64
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r client\python\requirements.lock
.\.venv\Scripts\python.exe -m pip install .\client\python
.\scripts\prepare-mcp.ps1
.\.venv\Scripts\dosbox-mcp.exe --agent-config tests\agent\mcp-test.env
```

The build produces `bin\x64\Agent Debug SDL2\dosbox-x.exe`. The package installs the `dosbox-mcp` command; it does not bundle the emulator. `requirements.lock` pins the tested Windows Python dependency set, including MCP 1.26.0. The package can also be installed editable with `pip install -e .\client\python` during development.

`prepare-mcp.ps1` creates the repository-owned deterministic fixtures in `tests\agent\runtime-mcp` and writes an explicit production configuration with absolute paths in `tests\agent\mcp-test.env`. To use your own programs, pass `-WorkDirectory 'D:\My DOS Programs'`; this writes `tests\agent\mcp-local.env`. No third-party executable is required or distributed. The checked-in [example configuration](../tests/agent/mcp-example.env) uses config-relative paths under the backend's test profile for portability.

The adapter fixes the bind address to `127.0.0.1`; `--port 8766` changes only the port. Each instance needs a distinct HTTP port and named-pipe endpoint. It reserves the HTTP port before launching the emulator and rejects an existing pipe endpoint. It waits for capabilities and an emulation-thread shell-readiness check before accepting HTTP requests. `--startup-timeout` defaults to 15 seconds.

The emulator starts with default configuration, fast launch, `waitonerror=false`, and `dosbox mcp_server=0`. This avoids local configuration prompts and leaves the older TCP debugger bridge disabled. Its implementation is unchanged.

## Connect Codex

Start the adapter first, then register it using the [official Codex MCP configuration mechanism](https://learn.chatgpt.com/docs/extend/mcp):

```powershell
codex mcp add dosbox --url http://127.0.0.1:8765/mcp
```

The equivalent user configuration is:

```toml
[mcp_servers.dosbox]
url = "http://127.0.0.1:8765/mcp"
startup_timeout_sec = 20
tool_timeout_sec = 15
```

No token or OAuth login is required. Restart or reconnect the MCP client after adding the server. A running adapter must be reachable from the same Windows computer as the client; a cloud-hosted client cannot reach this machine through its own loopback address.

For an isolated Codex acceptance run, without changing user configuration:

```powershell
.\.venv\Scripts\python.exe scripts\test-mcp-codex.py `
  --codex 'C:\path\to\codex.exe' --config tests\agent\mcp-test.env
```

This uses the installed Codex CLI's existing authentication and transient `-c mcp_servers.dosbox.url=...` settings. It runs the complete fixture workflow using MCP tools, records JSONL evidence, and shuts down the adapter. It requires network access for Codex inference.

## Agent workflow

1. Call `dosbox_agent_capabilities`. Check `debugger`, `trace.cpu`, `breakpoints.memory_change`, supported address spaces, and limits. `state.set_registers` and `code.disassemble` advertise the new features. `ready` indicates shell readiness for a new target; it can be false while a session is active.
2. Start the fixture with `dosbox_session_start` and `{"target":{"command":"AGENTFIX.COM","arguments":[]}}`. The backend mounts the configured controlled directory as `C:`, selects it, and uses the native entry breakpoint. Commands are DOS filenames, without drive prefixes or shell syntax. Preserve the returned `session_id`, `state_revision`, and stop address.
3. Read `dosbox_state_get_registers` and `dosbox_code_disassemble`. Disassembly defaults to current CS:EIP and 16 instructions. Every instruction includes a segmented address, `data_base64`, `length`, and native disassembly `text`. The result also contains `next_address` and `state_revision`.
4. Set execution breakpoints at verified instruction addresses. Continue returns an `operation_id` immediately. Poll `dosbox_execution_wait` with that ID and `timeout_ms` at most 1000. `running=true` means the wait elapsed without completion. It is not a stop or cancellation.
5. While running, query session status or request pause. Wait for pause completion before inspecting or changing state. Step with `mode="into"` or `mode="over"` while stopped; both return a register snapshot.
6. Read memory in segmented, linear, or physical space. Decode base64 as bytes. For writes, provide the preceding read's SHA-256 as `expected_sha256` and verify the resulting bytes and hash.
7. Edit registers with a nonempty map and `expected_state_revision` from the latest stopped state. Names are lowercase: `eax`, `ebx`, `ecx`, `edx`, `esi`, `edi`, `ebp`, `esp`, `eip`, `eflags`. Values are fixed-width 32-bit hex strings. Restore temporary changes using the revision returned by the edit. A rejected edit changes neither registers nor revision.
8. Trace a bounded instruction count, continue, wait for completion, and paginate `trace.read` using `next_cursor`. Debugger output uses its own cursor. `HELP`, `CPU`, and `PIC` are the only diagnostic commands. `accepted=true` with nonempty `raw_output` means the diagnostic succeeded. `parse_status=failed` refers to the optional register-text parser and is expected for output that is not a register dump; use the structured register tool for registers.
9. Stop the session and wait for `state="exited"`. Start another target in the same emulator if needed. Close the adapter with Ctrl+C when finished; client disconnect alone leaves the emulator and target running.

Register edits allow EFLAGS changes only to CF, PF, AF, ZF, SF, TF, IF, DF, and OF. Protected EFLAGS bits must retain their original values. EIP must fit the current code segment and execution mode. Segment, control, and FPU registers are excluded. Disassembly is bounded to 256 instructions; inaccessible selectors, segment boundaries, and missing pages return explicit address errors. Instructions truncated by an inaccessible boundary fail rather than inventing bytes; completed 16-bit instructions can wrap the next IP.

## Tool reference

All tools return the backend JSON object as MCP `structuredContent` plus a concise text summary. Hexadecimal segments are `0x` plus four digits; offsets/registers use eight digits. Session IDs, operation IDs, revisions, stop reasons, pagination cursors, and backend error codes are preserved. Each tool also accepts an optional explicit `request_id`.

| Tool | Required arguments beyond `session_id` | Effect |
|---|---|---|
| `dosbox_agent_capabilities` | No session or other arguments | Read capabilities, readiness and limits |
| `dosbox_session_start` | `target.command`, `target.arguments`; no session | Mount configured directory and stop target at entry |
| `dosbox_session_status` | None | Read state and last stop |
| `dosbox_session_stop` | None; optional `graceful_timeout_ms` | Terminate target; return operation ID |
| `dosbox_execution_continue` | None | Resume; return operation ID |
| `dosbox_execution_pause` | None | Request pause; return operation ID |
| `dosbox_execution_step` | `mode`: `into` or `over` | Step and return stopped state/registers |
| `dosbox_execution_wait` | `operation_id`, `timeout_ms` (0–1000, also bounded by config) | Wait without cancelling target |
| `dosbox_state_get_registers` | None | Read structured registers |
| `dosbox_state_set_registers` | `registers`, `expected_state_revision` | Atomically edit registers; increment revision once |
| `dosbox_code_disassemble` | None; optional segmented `address`, `instruction_count` | Read native instructions |
| `dosbox_memory_read` | `address`, `length` | Read base64 bytes and hash |
| `dosbox_memory_write` | `address`, `data_base64`; optional `expected_sha256` | Write guarded bytes |
| `dosbox_breakpoints_create` | `kind`, `address`, `once` | Create native execution/memory-change breakpoint |
| `dosbox_breakpoints_list` | None | Read stable IDs and addresses |
| `dosbox_breakpoints_delete` | `breakpoint_id` | Delete breakpoint |
| `dosbox_debug_output_read` | `cursor` (null initially), `limit` | Read debugger output page |
| `dosbox_debugger_execute_command` | `command`: `HELP`, `CPU`, `PIC` | Run diagnostic and collect output |
| `dosbox_trace_start` | `detail`: `normal`, `long`, `csip`; `instruction_count` | Start bounded native trace |
| `dosbox_trace_read` | `cursor` (null initially), `limit` | Read trace page |
| `dosbox_trace_stop` | None | Stop trace and report count |

Segmented addresses use `{"space":"segmented","segment":"0x0814","offset":"0x00000100"}`. Linear and physical addresses omit `segment` and set `space` accordingly. The controlled mount is fixed by configuration; the MCP start tool does not accept arbitrary host paths.

Errors set MCP `isError=true` and include `{"error":{"code":...,"message":...,"data":...}}` in structured content and text. Invalid arguments use `-32602`; backend errors retain their numeric code and reason, including `TARGET_RUNNING`, `CAPABILITY_UNAVAILABLE`, `ADDRESS_NOT_MAPPED`, `OPERATION_TIMEOUT`, and `MEMORY_PRECONDITION_FAILED`. Stale register writes return `-32018` / `STATE_REVISION_MISMATCH`. Address errors retain the failing address, offset, and cause.

Transport failure returns `BACKEND_DISCONNECTED` and permanently faults that adapter connection. Stop and restart the adapter explicitly after investigating. No mutation is automatically retried or replayed; an uncertain response may mean the mutation completed. Only an explicit retry with the same backend request ID and identical parameters can consult the backend's session-local completion cache.

## Tests and local programs

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s client\python\tests -t client\python -v
.\scripts\prepare-mcp.ps1
.\.venv\Scripts\python.exe client\python\tests\mcp_e2e.py --config tests\agent\mcp-test.env
```

The deterministic fixture run covers register edits/restoration, guarded memory writes, call/interrupt/REP stepping, memory-change breakpoints, disassembly boundaries, and protected mode with paging. It uses only test programs generated from source in this repository.

A separate automatic-launch display smoke test can use these verified DOSBox-X startup options, without the managed adapter running:

```powershell
& '.\bin\x64\Agent Debug SDL2\dosbox-x.exe' -defaultconf -fastlaunch `
  -c 'mount c "C:\DOS\Programs"' -c 'c:' -c 'PROGRAM.EXE'
```

For the debugger workflow, launch through `session.start` instead of adding a target filename to startup commands, so the agent can inspect the entry stop. Supply your own program and dependencies under the selected directory. No external game is needed for the acceptance suite.

Native tests must disable the debug build's console wait:

```powershell
$testProcess = Start-Process -FilePath (Resolve-Path 'bin\x64\Agent Debug SDL2\dosbox-x.exe') `
  -ArgumentList @('-tests','-defaultconf','-set','"sdl waitonerror=false"',
                  '--gtest_output=xml:tests/agent/mcp-native.xml') `
  -WindowStyle Hidden -Wait -PassThru
$testProcess.ExitCode
```

For optional-capability checks, build `Agent Debug No Heavy SDL2|x64` after the primary build, copy the generated fixture configuration to another explicit config, select that executable and a distinct pipe endpoint, and run `mcp_e2e.py --no-heavy --config <path> --port 18768`.

## Local access and lifecycle

The endpoint intentionally has no authentication and is available to local processes. HTTP Host must exactly match `127.0.0.1:<configured port>` or `localhost:<configured port>`. Browser Origin headers are rejected; no CORS middleware or wildcard policy is installed. Native MCP clients omit Origin. Do not proxy this endpoint onto another interface.

Pipe calls run on a single worker outside the HTTP event loop. Cancellation or client disconnect does not interrupt a mutation or start a duplicate. Wait requests are capped at one second so other control requests can make progress between polls. Process and pipe failures are reported, without silent restart.

On graceful adapter shutdown, queued requests are cancelled, the current bounded pipe call is allowed to finish, the active target is stopped when the connection is usable, the pipe closes, and the owned emulator is terminated. The process termination phase has a five-second deadline followed by kill and a one-second reap deadline; preceding pipe calls use the configured request timeout plus transport allowance. OS-level forced termination of the adapter cannot execute Python cleanup. This is not a headless guarantee or attach-to-existing-process service.

The fixture acceptance script records structured results locally. Generated logs and reports are excluded from the source submission.
