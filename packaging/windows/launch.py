"""Portable package entry point. Resolve every path from the extracted package."""
from pathlib import Path
import argparse
import hashlib
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workdir", type=Path, default=ROOT / "DOSDrive")
    parser.add_argument("--pipe-name", help="Optional unique local pipe name (letters, numbers, hyphen, underscore).")
    parser.add_argument("--startup-timeout", type=float, default=15)
    parser.add_argument("--write-config-only", action="store_true", help="Prepare a config for acceptance tests without starting a server.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 0 < args.startup_timeout <= 120:
        parser.error("port must be 1..65535; startup timeout must be >0 and <=120 seconds")
    workdir = args.workdir
    if not workdir.is_absolute():
        workdir = ROOT / workdir
    workdir = workdir.resolve()
    if not workdir.is_dir() or any(character in str(workdir) for character in '\r\n"'):
        parser.error("workdir must be an existing directory with no quotes/newlines in its path")
    executable = ROOT / "dosbox-x.exe"
    identity = hashlib.sha256((str(ROOT).casefold()+str(args.port)).encode()).hexdigest()[:12]
    pipe_name = args.pipe_name or f"dosbox-mcp-{identity}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", pipe_name):
        parser.error("invalid pipe name")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    config = runtime / f"agent-{args.port}.env"
    content = (
        f"transport=named_pipe\nendpoint=\\\\.\\pipe\\{pipe_name}\n"
        f"dosbox_executable={executable}\ndosbox_workdir={workdir}\nprofile=production\n"
        "request_timeout_ms=5000\nmax_message_bytes=1048576\nmax_memory_read_bytes=65536\nmax_trace_events=10000\n"
    )
    # Do not modify configuration underneath an already-running instance.
    if not config.is_file() or config.read_text(encoding="utf-8") != content:
        config.write_text(content, encoding="utf-8")
    if args.write_config_only:
        print(config)
        return 0
    print(f"DOSBox-X MCP: http://127.0.0.1:{args.port}/mcp", flush=True)
    print(f"Controlled C: directory: {workdir}", flush=True)
    print("Connect your MCP client, then start a DOS filename from the controlled directory. Ctrl+C stops the adapter.", flush=True)
    from dosbox_agent.mcp_server import main as server_main
    sys.argv = ["dosbox-mcp", "--agent-config", str(config), "--port", str(args.port),
                "--startup-timeout", str(args.startup_timeout)]
    server_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
