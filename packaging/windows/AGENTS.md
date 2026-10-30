# Debugging DOS programs in this package

Use the DOSBox MCP tools to control the bundled emulator. Read README.md first. The server is started by `start-mcp.cmd` and listens at `http://127.0.0.1:8765/mcp`. Connection configuration is in `codex-config.toml`. The complete tool reference and workflow are in `docs/mcp-agent-guide.md`.

- Call `dosbox_agent_capabilities` before using optional features.
- The controlled host directory defaults to this package's `DOSDrive`, mounted as guest `C:`. This directory is empty except for instructions; users supply their own programs.
- For a self-contained example, run `start-mcp.cmd --workdir testing\fixtures` and use `dosbox_session_start` with `{"target":{"command":"AGENTFIX.COM","arguments":[]}}`. Use the DOS filename without `C:\` or shell commands. It stops at entry for inspection.
- Preserve returned session IDs, operation IDs, revisions, stop reasons, and address formats. Read registers and structured disassembly before choosing instruction addresses.
- Continue and pause return operation IDs. Wait with bounded polls of at most 1000 ms. `running=true` is not an error or stop. Inspect/edit registers, code, memory and breakpoints only while stopped.
- Register edits require the current `expected_state_revision`. Restore temporary changes. Only the supported general registers/EIP and permitted EFLAGS bits may change; segment/control/FPU editing is excluded.
- Read before writing memory, supply the returned SHA-256 as `expected_sha256`, and verify the result. Instruction/memory bytes use base64.
- Prefer deterministic COM fixtures in `testing/fixtures` for deliberate memory mutations and call/interrupt stepping. To use them, restart with `start-mcp.cmd --workdir testing\fixtures`.
- Check trace and memory-change breakpoint capabilities. Keep traces bounded and use returned pagination cursors for trace/output.
- Only HELP, CPU and PIC diagnostic commands are available. `accepted=true` plus nonempty `raw_output` means command success; `parse_status=failed` is expected for diagnostic output that is not a register dump.
- Never automatically retry a mutation after a timeout/disconnection. Completion may be uncertain. The adapter does not silently restart the emulator or replay requests.
- Stop the target and wait for `exited` when finished. A new target can then start in the same emulator. Client disconnect alone does not stop the target.
- Keep debugger control agent-owned during managed sessions; simultaneous manual debugger editing is unsupported.

This is a Windows x64 agent/debug engineering build with a bundled Python runtime. It is intentionally unauthenticated on loopback. Keep the endpoint local. Acceptance uses repository-owned deterministic fixtures.
