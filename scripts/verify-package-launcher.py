"""Test the portable CLI, client disconnect, and graceful owned-process cleanup."""
import argparse
import asyncio
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--package", type=Path, required=True)
parser.add_argument("--port", type=int, default=18771)
args = parser.parse_args()
package = args.package.resolve()
runtime = package / ".runtime"
runtime.mkdir(exist_ok=True)
pid_file = runtime / "launcher-emulator-pid.txt"
log = runtime / "launcher-verification.log"
# The wrapper supplies a graceful SIGINT through stdin even when the build
# runner has no Windows console. It does not change the packaged launcher.
wrapper = """
import pathlib,runpy,signal,subprocess,sys,threading
from dosbox_agent import mcp_server
pid_path=pathlib.Path(sys.argv[1])
original=subprocess.Popen
def record(*args,**kwargs):
    process=original(*args,**kwargs)
    pid_path.write_text(str(process.pid))
    return process
subprocess.Popen=record
def stop():
    sys.stdin.readline()
    signal.raise_signal(signal.SIGINT)
threading.Thread(target=stop,daemon=True).start()
sys.argv=sys.argv[2:]
runpy.run_path(sys.argv[0],run_name='__main__')
"""
url = f"http://127.0.0.1:{args.port}/mcp"


async def inspect():
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool("dosbox_session_start", {"target": {"command": "AGENTRUN.COM", "arguments": []}})
            assert not result.isError, result
            sid = result.structuredContent["session_id"]
            result = await client.call_tool("dosbox_execution_continue", {"session_id": sid})
            assert not result.isError, result
    # A fresh client must still see the fixture running after disconnect.
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool("dosbox_session_status", {"session_id": sid})
            assert not result.isError and result.structuredContent["state"] == "running"


with log.open("w", encoding="utf-8") as output:
    process = subprocess.Popen([str(package / "python/python.exe"), "-c", wrapper, str(pid_file),
                                str(package / "adapter/launch.py"), "--port", str(args.port), "--workdir", "testing/fixtures"],
                               stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, cwd=package)
    try:
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Launcher exited during startup; inspect {log}")
            try:
                urllib.request.urlopen(url, timeout=1)
                break
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError("Launcher startup timed out")
        asyncio.run(inspect())
    finally:
        started = time.monotonic()
        process.communicate(input=b"stop\n", timeout=20)
        elapsed = time.monotonic()-started

assert "Application shutdown complete" in log.read_text(encoding="utf-8")
pid = int(pid_file.read_text())
query = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
                       capture_output=True, text=True)
assert not query.stdout.strip(), f"Emulator PID {pid} survived adapter shutdown"
print(f"Portable launcher, active-target disconnect, and graceful shutdown passed ({elapsed:.2f}s cleanup).")
