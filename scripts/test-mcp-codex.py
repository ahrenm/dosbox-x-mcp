"""Acceptance using Codex's configured HTTP MCP client; no user config changes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import threading
import uvicorn
from dosbox_agent.client import load_config
from dosbox_agent.mcp_server import ManagedBackend, create_app
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--codex", type=Path, required=True)
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--port", type=int, default=18767)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
url = f"http://127.0.0.1:{args.port}/mcp"
logpath = root / "tests/agent/mcp-codex-results.jsonl"
backend = ManagedBackend(load_config(args.config))
server = uvicorn.Server(uvicorn.Config(create_app(backend, args.port), host="127.0.0.1", port=args.port, log_level="warning"))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
try:
    deadline = time.monotonic()+20
    while time.monotonic() < deadline:
        if not thread.is_alive():
            raise RuntimeError("Adapter exited before readiness")
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except urllib.error.HTTPError:
            break # HTTP is accepting requests after successful ASGI startup.
        except OSError:
            time.sleep(.1)
    else:
        raise RuntimeError("Adapter startup timeout")
    prompt = """Test the dosbox MCP connection using only its MCP tools, without shell commands or file edits.
Discover capabilities. Start AGENTFIX.COM with an empty arguments list. Inspect registers and disassemble 3 instructions.
Set EAX to 0x12345678 using the current state revision, then restore its previous value using the new revision.
Set a breakpoint at entry CS:0x00000106, continue and wait in bounded polls, inspect the stop, then delete the breakpoint.
Step into once and step over once. Read 4 bytes at DS:0x00000200; write the same bytes back with the returned expected_sha256.
Run CPU diagnostics and read debugger output. Diagnostic success means accepted=true with nonempty raw_output;
parse_status=failed is expected for CPU diagnostic text because it is not a register dump.
Start a 2-instruction normal trace, continue/wait, read trace and stop tracing.
Stop the session and wait for exited. Start AGENTFIX.COM again and stop/wait again.
Report PASS only if every requested operation succeeds. Never retry a mutation after uncertain completion."""
    command = [str(args.codex), "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--json",
               "-s", "read-only", "-C", str(root),
               "-c", f'mcp_servers.dosbox.url="{url}"',
               "-c", 'mcp_servers.dosbox.default_tools_approval_mode="approve"',
               "-c", "mcp_servers.dosbox.tool_timeout_sec=15", prompt]
    with logpath.open("w", encoding="utf-8") as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=240, cwd=root)
    records = logpath.read_text(encoding="utf-8")
    print(f"Codex exit={result.returncode}; evidence={logpath}")
    if result.returncode:
        print(records[-4000:])
    messages = []
    for line in records.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item", {})
        if item.get("type") == "agent_message":
            messages.append(item.get("text", ""))
    passed = messages and messages[-1].lstrip().startswith("PASS")
    print(messages[-1] if messages else "No final Codex message")
    raise SystemExit(result.returncode or (0 if passed else 1))
finally:
    server.should_exit = True
    thread.join(timeout=20)
    if thread.is_alive():
        raise RuntimeError("Adapter shutdown timed out")
