from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from dosbox_agent.client import load_config
from dosbox_agent.errors import AgentConnectionError, AgentRpcError
from dosbox_agent.mcp_server import ManagedBackend, METHODS, create_app, tools

ROOT = Path(__file__).resolve().parents[3]


class FakeBackend:
    config = load_config(ROOT / "tests/agent/agent-test.env")

    def __init__(self):
        self.started = False
        self.closed = False
        self.calls = []
        self.error = None

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    async def dispatch(self, method, params, request_id=None):
        self.calls.append((method, params, request_id))
        if self.error:
            raise self.error
        return {"session_id": "ses-1", "state_revision": 3, "state": "stopped"}


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = FakeBackend()
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.socket.setblocking(False)
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.backend, self.port), log_level="error"))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        for _ in range(200):
            if self.server.started:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.server.started)
        self.url = f"http://127.0.0.1:{self.port}/mcp"

    async def asyncTearDown(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.task, 5)
        self.socket.close()
        self.assertTrue(self.backend.closed)

    async def test_official_client_initialization_discovery_results_and_errors(self):
        async with streamable_http_client(self.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                self.assertEqual(initialized.serverInfo.name, "dosbox-x")
                discovered = (await session.list_tools()).tools
                self.assertEqual({t.name for t in discovered}, {"dosbox_"+m.replace('.', '_') for m in METHODS})
                for tool in discovered:
                    Draft202012Validator.check_schema(tool.inputSchema)
                    self.assertFalse(tool.annotations.openWorldHint)
                result = await session.call_tool("dosbox_state_get_registers", {"session_id": "ses-1"})
                self.assertFalse(result.isError)
                self.assertEqual(result.structuredContent["state_revision"], 3)
                self.assertIn("state_revision=3", result.content[0].text)
                result = await session.call_tool("dosbox_state_set_registers", {
                    "session_id": "ses-1", "registers": {"cr0": "0x00000000"}, "expected_state_revision": 1})
                self.assertTrue(result.isError)
                self.assertEqual(result.structuredContent["error"]["code"], -32602)
                self.backend.error = AgentRpcError(-32014, "unmapped", {"reason": "ADDRESS_NOT_MAPPED", "cause": "page_not_present"})
                result = await session.call_tool("dosbox_state_get_registers", {"session_id": "ses-1"})
                self.assertEqual(result.structuredContent["error"]["data"]["cause"], "page_not_present")
                self.backend.error = AgentConnectionError("emulator exited")
                result = await session.call_tool("dosbox_state_get_registers", {"session_id": "ses-1"})
                self.assertEqual(result.structuredContent["error"]["code"], "BACKEND_DISCONNECTED")
        self.assertFalse(self.backend.closed, "MCP disconnect must not stop emulator")

    async def test_host_origin_and_cors(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
        async with httpx.AsyncClient() as client:
            headers = {"Accept": "application/json, text/event-stream"}
            result = await client.post(self.url, json=payload, headers={**headers, "Host": "evil.example"})
            self.assertEqual(result.status_code, 421)
            for origin in ("https://evil.example", "null", f"http://127.0.0.1:{self.port}"):
                result = await client.post(self.url, json=payload, headers={**headers, "Origin": origin})
                self.assertEqual(result.status_code, 403)
            result = await client.post(self.url, json=payload, headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertNotIn("access-control-allow-origin", result.headers)

    async def test_wait_and_diagnostic_bounds(self):
        async with streamable_http_client(self.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for name, args in (
                    ("dosbox_execution_wait", {"session_id": "ses-1", "operation_id": "op-1", "timeout_ms": 1001}),
                    ("dosbox_debugger_execute_command", {"session_id": "ses-1", "command": "RUN"}),
                    ("dosbox_code_disassemble", {"session_id": "ses-1", "instruction_count": 257}),
                ):
                    self.assertTrue((await session.call_tool(name, args)).isError)
                self.assertEqual(self.backend.calls, [])


class LifecycleTests(unittest.TestCase):
    def test_port_collision_fails_before_launch(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            result = subprocess.run([sys.executable, "-m", "dosbox_agent.mcp_server", "--agent-config", str(FakeBackend.config.path),
                                     "--port", str(listener.getsockname()[1])], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bind", result.stderr)

    def test_shutdown_kills_an_unresponsive_owned_process(self):
        backend = ManagedBackend(FakeBackend.config)
        backend.process = Mock()
        backend.process.poll.return_value = None
        backend.process.wait.side_effect = [subprocess.TimeoutExpired("dosbox", 5), 0]
        backend.close()
        backend.process.kill.assert_called_once()

    def test_startup_timeout_cleans_up_owned_process(self):
        backend = ManagedBackend(FakeBackend.config, startup_timeout=.01)
        process = Mock()
        process.poll.return_value = None
        with patch("dosbox_agent.mcp_server._pipe_exists", return_value=False), patch("subprocess.Popen", return_value=process), \
             patch.object(Path, "is_file", return_value=True), patch.object(Path, "is_dir", return_value=True):
            with self.assertRaisesRegex(AgentConnectionError, "Timed out"):
                backend.start()
        process.terminate.assert_called_once()

    def test_disconnect_never_replays_mutation(self):
        backend = ManagedBackend(FakeBackend.config)
        backend.process = Mock()
        backend.process.poll.return_value = None
        backend.client = Mock()
        backend.client.call.side_effect = AgentConnectionError("lost response")
        with self.assertRaises(AgentConnectionError):
            backend.call("memory.write", {"data_base64": "AQ=="})
        with self.assertRaises(AgentConnectionError):
            backend.call("memory.write", {"data_base64": "AQ=="})
        self.assertEqual(backend.client.call.call_count, 1)
        backend.close()
        backend.process.terminate.assert_called_once()

    def test_endpoint_collision_prevents_process_launch(self):
        backend = ManagedBackend(FakeBackend.config)
        with patch("dosbox_agent.mcp_server._pipe_exists", return_value=True), patch("subprocess.Popen") as launch, \
             patch.object(Path, "is_file", return_value=True), patch.object(Path, "is_dir", return_value=True):
            with self.assertRaisesRegex(AgentConnectionError, "already in use"):
                backend.start()
            launch.assert_not_called()
        backend.close()

    def test_startup_exit_is_reported_and_closed(self):
        backend = ManagedBackend(FakeBackend.config)
        process = Mock()
        process.poll.return_value = 7
        with patch("dosbox_agent.mcp_server._pipe_exists", return_value=False), patch("subprocess.Popen", return_value=process), \
             patch.object(Path, "is_file", return_value=True), patch.object(Path, "is_dir", return_value=True):
            with self.assertRaisesRegex(AgentConnectionError, "startup"):
                backend.start()
        process.terminate.assert_not_called()

    def test_mutation_annotations(self):
        definitions = tools(FakeBackend.config)
        self.assertFalse(definitions["dosbox_state_set_registers"].annotations.readOnlyHint)
        self.assertTrue(definitions["dosbox_code_disassemble"].annotations.readOnlyHint)


class SerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipe_calls_are_serial_and_do_not_block_http_event_loop(self):
        backend = ManagedBackend(FakeBackend.config)
        backend.process = Mock()
        backend.process.poll.return_value = None
        backend.client = Mock()
        active = 0
        peak = 0

        def blocked_call(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(.1)
            active -= 1
            return {"state_revision": 1}

        backend.client.call.side_effect = blocked_call
        first = asyncio.create_task(backend.dispatch("memory.write", {}))
        second = asyncio.create_task(backend.dispatch("state.get_registers", {}))
        started = time.monotonic()
        await asyncio.sleep(.02)
        self.assertLess(time.monotonic()-started, .09)
        await asyncio.gather(first, second)
        self.assertEqual(peak, 1)
        self.assertEqual(backend.client.call.call_count, 2)
        backend.close()


if __name__ == "__main__":
    unittest.main()
