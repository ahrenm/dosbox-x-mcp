# Portable Windows package

The package follows the runtime layout of the user-provided DOSBox-X reference release while using this repository's Agent Debug SDL2 x64 executable. Its Python adapter and dependencies are bundled with portable CPython 3.13.17, so running the extracted package does not require Python installation or pip access.

## Build inputs

- Build `Agent Debug SDL2|x64` as described in [the agent guide](mcp-agent-guide.md).
- Use the repository virtual environment with pip and the pinned [dependency lock](../client/python/requirements.lock).
- Supply a reference DOSBox-X runtime release explicitly with `--reference <path>`.
- Use repository-owned fixtures for acceptance. Local user programs are optional and are never copied into packages.
- The packager downloads the [official Python Windows embeddable runtime](https://www.python.org/downloads/windows/) and Windows CPython 3.13 dependency wheels when they are not cached. The runtime archive URL and SHA-256 are pinned in the packaging script. Wheel hashes and the lock hash are recorded in `BUILD-INFO.json`.

## Assemble and verify

```powershell
.\.venv\Scripts\python.exe scripts\package-windows.py --reference 'D:\Path\To\DOSBox-X' --stage-only
```

The default folder is `artifacts\releases\DOSBox-X-MCP-0.2.0-win64`. Existing folders are not deleted or overwritten. Select `--output <new-path-under-repository>` for another build. `--reference <path>` selects a different reference installation. The reference release is only read.

Run the extracted-package commands in its README using `python\python.exe`, which is the bundled interpreter. Test the repository-owned fixtures through the official MCP client. For relocation verification, copy/extract to another path containing spaces and repeat the fixture test there; generated configs must point to that extracted folder, not to this repository's build or virtual environment.

After validation, put a concise result record in `verification/` beneath the staged folder, then archive it:

```powershell
.\.venv\Scripts\python.exe scripts\package-windows.py --archive-only
```

Outputs are the portable ZIP, a matching SHA-256 file, a separate corresponding-source ZIP, and its SHA-256 file. The source archive contains tracked repository files and the new adapter, documentation, tests and packaging source from this working tree. It excludes build outputs, private Git metadata, local runtime directories and unrelated untracked binary files. `BUILD-INFO.json` records the base commit and that local changes are included.

The portable ZIP omits generated `.runtime` configurations and Python bytecode. Its `SHA256SUMS.txt` covers shipped files except itself. Runtime dependencies retain their distribution metadata and licenses. Python's license is included in its runtime directory; DOSBox-X's COPYING is at the package root. Preserve these files and the corresponding source archive when passing the build on.

## Layout and scope

The package includes shaders, language files, fonts, drive-Z support and utility scripts from the reference release. It excludes the reference executable, machine-specific `dosbox-x.conf`, and installer/uninstaller. No third-party game or application files are copied. `DOSDrive` contains only a README for user-supplied programs, and archive creation excludes any local program subsequently placed there.

The bundled emulator is the tested agent/debug build rather than an optimized stock release. Its native dependencies are Windows system libraries; the packaging verification also checks the portable Python and its compiled extensions. No debug CRT DLLs, Visual Studio installation, or global Python packages are used by the package.

`start-mcp.cmd` resolves files from its own location and generates an explicit production agent config under `.runtime`. `--workdir`, `--port`, and `--pipe-name` select the controlled directory and instance identifiers. Agent instructions live at root `AGENTS.md`, and `codex-config.toml` supplies the loopback HTTP connection example.

## Package verification

The acceptance script uses only the deterministic fixtures generated from this repository. Verify a staged package and a relocated copy using its bundled interpreter and the commands in its README. Compiled Pydantic, CFFI, cryptography and pywin32 extensions should load without a global Python installation.

The launcher verification starts the infinite-loop fixture, disconnects the client, and reconnects to confirm that it is still running. It then interrupts the adapter and confirms that its owned emulator exits. The wrapper supplies SIGINT through stdin for runners without an interactive console:

```powershell
.\.venv\Scripts\python.exe scripts\verify-package-launcher.py `
  --package 'artifacts\verification\Portable Build With Spaces'
```

Run the native suite and Python client/HTTP tests alongside these fixture and launcher checks. No third-party binary is part of acceptance or packaging.

The cleaned package was staged and verified using only the repository fixtures: 100 recorded MCP operations and the active-target disconnect/shutdown check passed. Its `DOSDrive` contained only `README.txt`. Two packaging regression tests cover exclusion of local user files from both runtime and source archives.
