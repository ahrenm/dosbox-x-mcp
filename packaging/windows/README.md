# DOSBox-X MCP — portable Windows x64 agent build

Extract the entire folder into a writable location. No Python installation or package download is required to run it. This engineering build includes the native debugger and CPU tracing; it is larger than a normal DOSBox-X release.

## Start debugging

1. Run `start-mcp.cmd --workdir testing\fixtures` (or execute it in a terminal). It starts the bundled emulator and serves MCP at `http://127.0.0.1:8765/mcp`.
2. Connect Codex with `codex mcp add dosbox --url http://127.0.0.1:8765/mcp`. The equivalent configuration is in `codex-config.toml`. Reconnect/restart the client after registering.
3. Give the agent `AGENTS.md`. Ask it to launch `AGENTFIX.COM` with an empty arguments list, inspect entry registers/instructions, and single-step the fixture. To debug your own programs, put them under `DOSDrive` and run `start-mcp.cmd` without `--workdir`; this folder becomes guest drive `C:`.
4. Press Ctrl+C in the adapter terminal to stop it and its owned emulator. Disconnecting the MCP client leaves the target running.

Examples:

```powershell
.\start-mcp.cmd
.\start-mcp.cmd --port 8766
.\start-mcp.cmd --workdir 'D:\My DOS Programs'
.\start-mcp.cmd --workdir testing\fixtures
```

Each port gets a distinct local pipe endpoint derived from the extracted package path. Use `--pipe-name <unique-name>` to override it. The endpoint always binds to IPv4 loopback. All browser Origin headers are rejected; there is no wildcard CORS or authentication token. The work directory is fixed for the lifetime of an instance. Put all target dependencies beneath it.

Paths are resolved from the extracted folder; the package can be moved. Its `.runtime` directory contains generated absolute-path configurations and test results and can be recreated on the next launch. Keep the package path free of quotes/newlines.

## Included files

- `dosbox-x.exe`: our Windows x64 Agent Debug SDL2 build with MCP backend extensions.
- `adapter/dosbox_agent`: Python client and MCP adapter source.
- `python`: portable CPython and pinned third-party runtime dependencies, including official MCP SDK 1.26.0.
- `AGENTS.md`: instructions for an AI debugger agent.
- `docs`: full workflow and RPC extension contract. Build/install sections describe the source repository; this README describes the portable package.
- `DOSDrive/README.txt`: instructions for adding your own DOS programs. No third-party executable is bundled.
- `testing/fixtures` and `testing/mcp_e2e.py`: deterministic targets and official-MCP-client acceptance script.
- DOSBox-X shaders, languages, fonts and utility files matching the reference release layout.
- `BUILD-INFO.json` and `SHA256SUMS.txt`: build provenance and package file hashes.
- `COPYING.txt`, `README.upstream.txt`, and dependency distribution metadata: bundled notices/licenses.

The reference installation is used for runtime assets only. Its executable, installer/uninstaller, and machine-specific configuration are not copied. Third-party game/application files are not included.

## Verify this extracted package

Close other package servers before these tests. The tests start and stop their own emulator.

```powershell
.\python\python.exe adapter\launch.py --port 18765 --workdir testing\fixtures --write-config-only
.\python\python.exe testing\mcp_e2e.py --config .runtime\agent-18765.env --port 18765
```

The neighboring `-source.zip` contains the corresponding source snapshot, build files, adapter, packaging script, and documentation. It includes uncommitted changes used for this local build. See `docs/windows-package.md` for the reproducible package recipe.
