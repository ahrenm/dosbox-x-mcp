"""Owned DOSBox-X process and typed, loopback-only Streamable HTTP tools."""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import socket
import subprocess
import threading
import time
from typing import Any
import uuid

from jsonschema import Draft202012Validator, ValidationError
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route
import uvicorn

from .client import AgentClient, load_config
from .errors import AgentConnectionError, AgentProtocolError, AgentRpcError
from .models import AgentConfig

LOG = logging.getLogger(__name__)
TEXT = {"type": "string", "minLength": 1}
HEX32 = {"type": "string", "pattern": "^0x[0-9a-fA-F]{8}$"}
HEX16 = {"type": "string", "pattern": "^0x[0-9a-fA-F]{4}$"}
ADDRESS = {"oneOf": [
    {"type": "object", "properties": {"space": {"const": "segmented"}, "segment": HEX16, "offset": HEX32},
     "required": ["space", "segment", "offset"], "additionalProperties": False},
    {"type": "object", "properties": {"space": {"enum": ["linear", "physical"]}, "offset": HEX32},
     "required": ["space", "offset"], "additionalProperties": False},
]}


def integer(minimum: int, maximum: int) -> dict:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def tools(config: AgentConfig) -> dict[str, types.Tool]:
    """The closed registry is the only RPC allowlist exposed over HTTP."""
    definitions: dict[str, types.Tool] = {}

    def add(method: str, description: str, properties: dict, required: list[str], *, read=False, session=True):
        if session:
            properties = {"session_id": TEXT, **properties}
            required = ["session_id", *required]
        properties = {**properties, "request_id": {**TEXT, "description": "Optional explicit backend request ID; never reuse with a different payload."}}
        name = "dosbox_" + method.replace(".", "_")
        definitions[name] = types.Tool(
            name=name, description=description,
            inputSchema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            outputSchema={"type": "object", "additionalProperties": True},
            annotations=types.ToolAnnotations(readOnlyHint=read, destructiveHint=not read,
                                               idempotentHint=read, openWorldHint=False),
        )

    add("agent.capabilities", "Discover debugger capabilities and configured limits before using optional features.", {}, [], read=True, session=False)
    add("session.start", "Mount the configured controlled directory as C: and launch a DOS filename stopped at entry. One active session per adapter.", {
        "target": {"type": "object", "properties": {"command": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+$"},
                    "arguments": {"type": "array", "items": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+$"}}},
                   "required": ["command", "arguments"], "additionalProperties": False}}, ["target"], session=False)
    add("session.status", "Read session state, revision, and last stop reason. Safe while running.", {}, [], read=True)
    add("session.stop", "Terminate the active DOS target; wait using the returned operation ID.", {"graceful_timeout_ms": integer(0, config.request_timeout_ms)}, [])
    add("execution.continue", "Resume a stopped target; return an operation ID without waiting for a stop.", {}, [])
    add("execution.pause", "Request a debugger stop; wait using the returned operation ID.", {}, [])
    add("execution.step", "Step a stopped target into or over one instruction/call/interrupt; returns stopped registers.", {"mode": {"enum": ["into", "over"]}}, ["mode"])
    add("execution.wait", "Bounded wait for an operation. running=true means no stop yet; does not cancel execution.", {
        "operation_id": TEXT, "timeout_ms": integer(0, min(1000, config.request_timeout_ms))}, ["operation_id", "timeout_ms"], read=True)
    add("state.get_registers", "Read structured general and segment registers, EIP, EFLAGS, execution mode and revision. Target must be stopped.", {}, [], read=True)
    add("state.set_registers", "Atomically edit general registers/EIP/EFLAGS on a stopped target at the expected revision. Only arithmetic/control flag bits CF/PF/AF/ZF/SF/TF/IF/DF/OF may change.", {
        "registers": {"type": "object", "properties": {name: HEX32 for name in
                      ("eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp", "eip", "eflags")},
                      "minProperties": 1, "additionalProperties": False},
        "expected_state_revision": integer(0, 2**64-1)}, ["registers", "expected_state_revision"])
    add("code.disassemble", "Disassemble stopped guest code with native mode-aware decoding. Defaults to current CS:EIP and 16 instructions; maximum 256. Bytes are base64.", {
        "address": ADDRESS["oneOf"][0], "instruction_count": integer(1, 256)}, [], read=True)
    add("memory.read", "Read stopped guest memory in segmented, linear or physical space; return base64 bytes and SHA-256.", {
        "address": ADDRESS, "length": integer(1, config.max_memory_read_bytes)}, ["address", "length"], read=True)
    add("memory.write", "Write stopped guest memory. Supply the SHA-256 from memory.read to guard against stale bytes. Never automatically retry after uncertain completion.", {
        "address": ADDRESS, "data_base64": {"type": "string", "minLength": 4, "maxLength": ((config.max_memory_read_bytes+2)//3)*4},
        "expected_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}}, ["address", "data_base64"])
    add("breakpoints.create", "Create a stopped-target execution or memory-change breakpoint. Check capabilities for supported kinds/address spaces.", {
        "kind": {"enum": ["execution", "memory_change"]}, "address": ADDRESS, "once": {"type": "boolean"}}, ["kind", "address", "once"])
    add("breakpoints.list", "List stable breakpoint IDs and addresses.", {}, [], read=True)
    add("breakpoints.delete", "Delete one breakpoint by its stable ID.", {"breakpoint_id": TEXT}, ["breakpoint_id"])
    page = {"cursor": {"type": ["string", "null"]}, "limit": integer(1, min(256, config.max_trace_events))}
    add("debug.output.read", "Read a bounded debugger output page; use next_cursor for subsequent pages.", page, ["cursor", "limit"], read=True)
    add("debugger.execute_command", "Run one allowlisted diagnostic command: HELP, CPU or PIC. Requires a stopped target. accepted=true and nonempty raw_output mean success. parse_status describes only the optional register-text parser; failed is expected for diagnostic text that is not a register dump.", {"command": {"enum": ["HELP", "CPU", "PIC"]}}, ["command"])
    add("trace.start", "Start bounded CPU instruction tracing; requires trace.cpu capability and a stopped target.", {
        "detail": {"enum": ["normal", "long", "csip"]}, "instruction_count": integer(1, config.max_trace_events)}, ["detail", "instruction_count"])
    add("trace.read", "Read a bounded session-local trace page, including next_cursor and active state.", page, ["cursor", "limit"], read=True)
    add("trace.stop", "Stop CPU tracing and return the collected event count.", {}, [])
    return definitions


class ManagedBackend:
    def __init__(self, config: AgentConfig, startup_timeout: float = 15, shutdown_timeout: float = 5):
        if not config.endpoint.startswith("\\\\.\\pipe\\") or not config.endpoint[9:]:
            raise ValueError("MCP requires an explicit local Windows named-pipe endpoint")
        self.config = config
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.client = AgentClient(config)
        self.process: subprocess.Popen | None = None
        self.session_id: str | None = None
        self.failure: str | None = None
        self.capabilities: dict = {}
        self._lock = threading.Lock()
        # One worker means cancellation/disconnect cannot overlap pipe mutations.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dosbox-pipe")
        self._prefix = uuid.uuid4().hex
        self._sequence = 0

    async def dispatch(self, method: str, params: dict, request_id: str | None = None) -> dict:
        loop = asyncio.get_running_loop()
        return await asyncio.shield(loop.run_in_executor(self._executor, self.call, method, params, request_id))

    def start(self):
        if not self.config.dosbox_executable.is_file() or not self.config.dosbox_workdir.is_dir():
            raise ValueError("Configured emulator executable and controlled work directory must exist")
        # An existing pipe must not be mistaken for the process we are about to own.
        if _pipe_exists(self.config.endpoint):
            raise AgentConnectionError("Configured pipe endpoint is already in use")
        self.process = subprocess.Popen([
            str(self.config.dosbox_executable), "--agent-config", str(self.config.path),
            "-defaultconf", "-fastlaunch", "-set", "sdl waitonerror=false",
            "-set", "dosbox mcp_server=0",
        ], cwd=self.config.dosbox_workdir, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + self.startup_timeout
        try:
            while not _pipe_exists(self.config.endpoint):
                if self.process.poll() is not None:
                    raise AgentConnectionError(f"Emulator exited during startup (exit {self.process.returncode})")
                if time.monotonic() >= deadline:
                    raise AgentConnectionError("Timed out waiting for emulator pipe readiness")
                time.sleep(0.025)
            self.capabilities = self.call("agent.capabilities", {})
            if not self.capabilities.get("debugger"):
                raise AgentConnectionError("Configured emulator has no debugger capability")
            # Round-trip through the emulation queue, not just the pipe worker.
            # readiness is included by the extended backend, older backends are rejected.
            while not self.capabilities.get("ready"):
                if time.monotonic() >= deadline:
                    raise AgentConnectionError("Timed out waiting for DOS shell readiness")
                time.sleep(0.05)
                self.capabilities = self.call("agent.capabilities", {})
        except BaseException:
            self.close()
            raise

    def call(self, method: str, params: dict, request_id: str | None = None) -> dict:
        with self._lock:
            if self.failure:
                raise AgentConnectionError(self.failure)
            if self.process is None or self.process.poll() is not None:
                raise AgentConnectionError("Owned emulator is not running; restart the adapter explicitly")
            if method == "session.start":
                params = {**params, "mounts": [{"drive": "C", "host_path": str(self.config.dosbox_workdir)}], "break_at": "entry"}
            self._sequence += 1
            identifier = request_id or f"mcp-{self._prefix}-{self._sequence}"
            try:
                result = self.client.call(method, params or None, identifier)
            except (AgentConnectionError, AgentProtocolError, OSError) as error:
                self.failure = f"Backend disconnected or response uncertain: {error}. No request was replayed; restart adapter explicitly."
                self.client.close()
                raise AgentConnectionError(self.failure) from error
            if method == "session.start":
                self.session_id = result["session_id"]
            return result

    def close(self):
        # Queued calls are bounded by the transport timeout; shutdown does not replay them.
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            deadline = time.monotonic() + self.shutdown_timeout
            if self.process is not None and self.process.poll() is None:
                if self.session_id and not self.failure:
                    try:
                        self.client.stop(self.session_id)
                    except Exception:
                        LOG.exception("Unable to stop guest before emulator termination")
                self.process.terminate()
                try:
                    self.process.wait(timeout=max(0.01, deadline-time.monotonic()))
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
            self.client.close()


def _pipe_exists(endpoint: str) -> bool:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    wait = kernel.WaitNamedPipeW
    wait.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    wait.restype = wintypes.BOOL
    if wait(endpoint, 1):
        return True
    code = ctypes.get_last_error()
    if code in (121, 231):
        return True
    if code in (2, 3):
        return False
    raise AgentConnectionError(f"Cannot inspect configured pipe: Win32 error {code}")


def error_result(code: int | str, message: str, data: dict) -> types.CallToolResult:
    result = {"error": {"code": code, "message": message, "data": data}}
    return types.CallToolResult(isError=True, structuredContent=result,
                               content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))])


def create_app(backend: ManagedBackend, port: int = 8765) -> Starlette:
    definitions = tools(backend.config)
    server = Server("dosbox-x", version="0.2.0", instructions="Agent-owned debugger. Discover capabilities; start a DOS filename, inspect the entry stop, then continue/wait. No automatic mutation retries.")

    @server.list_tools()
    async def list_tools():
        return list(definitions.values())

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict):
        definition = definitions.get(name)
        if definition is None:
            return error_result(-32601, "Unknown tool", {})
        try:
            Draft202012Validator(definition.inputSchema).validate(arguments)
        except ValidationError as error:
            return error_result(-32602, error.message, {"reason": "INVALID_ARGUMENTS", "path": list(error.path)})
        params = dict(arguments)
        request_id = params.pop("request_id", None)
        # Register names and execute_command contain underscores; use registry mapping.
        method = next(m for m in METHODS if "dosbox_" + m.replace(".", "_") == name)
        try:
            result = await backend.dispatch(method, params, request_id)
            summary = f"{method}: " + ", ".join(f"{key}={result[key]}" for key in
                ("session_id", "operation_id", "state", "state_revision", "running", "byte_count", "next_cursor") if key in result)
            return types.CallToolResult(structuredContent=result, content=[types.TextContent(type="text", text=summary)])
        except AgentRpcError as error:
            return error_result(error.code, error.message, error.data)
        except (AgentConnectionError, AgentProtocolError, OSError) as error:
            return error_result("BACKEND_DISCONNECTED", str(error), {"reason": "BACKEND_DISCONNECTED"})

    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True,
        security_settings=TransportSecuritySettings(allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"], allowed_origins=[]))

    @asynccontextmanager
    async def lifespan(app):
        try:
            await asyncio.to_thread(backend.start)
            async with manager.run():
                yield
        finally:
            await asyncio.to_thread(backend.close)

    class Endpoint:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    return Starlette(routes=[Route("/mcp", endpoint=Endpoint(), methods=["GET", "POST", "DELETE"])], lifespan=lifespan)


METHODS = ("agent.capabilities", "session.start", "session.status", "session.stop", "execution.continue", "execution.pause",
           "execution.step", "execution.wait", "state.get_registers", "state.set_registers", "code.disassemble", "memory.read",
           "memory.write", "breakpoints.create", "breakpoints.list", "breakpoints.delete", "debug.output.read",
           "debugger.execute_command", "trace.start", "trace.read", "trace.stop")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-config", required=True, type=Path)
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--startup-timeout", default=15.0, type=float)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 0 < args.startup_timeout <= 120:
        parser.error("port must be 1..65535 and startup timeout must be >0 and <=120 seconds")
    logging.basicConfig(level=logging.INFO)
    backend = ManagedBackend(load_config(args.agent_config), args.startup_timeout)
    # Reserve HTTP port before spawning emulator, so collisions never launch a target.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", args.port))
        listener.listen(64)
        listener.setblocking(False)
        uvicorn.Server(uvicorn.Config(create_app(backend, args.port), host="127.0.0.1", port=args.port,
                                     timeout_graceful_shutdown=10)).run(sockets=[listener])


if __name__ == "__main__":
    main()
