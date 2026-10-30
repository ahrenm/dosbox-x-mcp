"""Real Windows emulator acceptance through the official MCP Python client.

Run with --config tests/agent/mcp-test.env after scripts/prepare-mcp.ps1.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import json
from pathlib import Path
import socket
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn
from dosbox_agent.client import load_config
from dosbox_agent.mcp_server import ManagedBackend, create_app

async def run(config: Path, port: int, no_heavy: bool=False):
    backend = ManagedBackend(load_config(config))
    listener = socket.socket()
    listener.bind(('127.0.0.1', port))
    listener.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(create_app(backend, port), log_level='error'))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    evidence = []
    try:
        for _ in range(2000):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError('MCP server failed startup')
            await asyncio.sleep(0.01)
        assert server.started, 'MCP server did not become ready'
        async with streamable_http_client(f'http://127.0.0.1:{port}/mcp') as (read, write, _):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                evidence.append({'server': info.serverInfo.model_dump()})
                assert len((await session.list_tools()).tools) == 21

                async def call(method, params=None, error=None):
                    started = time.monotonic()
                    result = await session.call_tool('dosbox_' + method.replace('.', '_'), params or {})
                    data = result.structuredContent
                    evidence.append({'method': method, 'seconds': round(time.monotonic() - started, 3), 'result': data})
                    if error is not None:
                        assert result.isError, (method, data)
                        fault = data['error']
                        assert fault['code'] == error or fault['data'].get('reason') == error, fault
                    else:
                        assert not result.isError, (method, data)
                    return data

                async def wait(sid, op):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        result = await call('execution.wait', {'session_id': sid, 'operation_id': op['operation_id'], 'timeout_ms': 500})
                        if not result.get('running'):
                            return result
                    raise AssertionError('operation did not complete within deadline')

                async def start(command):
                    return await call('session.start', {'target': {'command': command, 'arguments': []}})

                async def stop(sid):
                    result = await wait(sid, await call('session.stop', {'session_id': sid}))
                    assert result['state'] == 'exited'
                caps = await call('agent.capabilities')
                assert caps['ready'] and caps['state']['set_registers'] and caps['code']['disassemble']
                if no_heavy:
                    assert caps['trace']['cpu'] is False
                    assert caps['breakpoints']['memory_change'] is False
                    entry = await start('AGENTFIX.COM')
                    args = {'session_id': entry['session_id']}
                    await call('trace.start', {**args, 'detail': 'normal', 'instruction_count': 1}, 'CAPABILITY_UNAVAILABLE')
                    await call('breakpoints.create', {**args, 'kind': 'memory_change', 'address': entry['stop_reason']['address'], 'once': False}, 'CAPABILITY_UNAVAILABLE')
                    await call('code.disassemble', {**args, 'instruction_count': 1})
                    await stop(entry['session_id'])
                    backend.process.kill()
                    backend.process.wait(timeout=3)
                    await call('agent.capabilities', {}, 'BACKEND_DISCONNECTED')
                    print('MCP non-heavy capability and emulator-exit acceptance passed.')
                    return
                entry = await start('AGENTFIX.COM')
                sid = entry['session_id']
                args = {'session_id': sid}
                regs = await call('state.get_registers', args)
                code = await call('code.disassemble', {**args, 'instruction_count': 3})
                assert len(code['instructions']) == 3
                assert base64.b64decode(code['instructions'][0]['data_base64']) == b'\xb84\x12'
                assert code['instructions'][0]['length'] == 3
                assert code['next_address']['offset'] == '0x00000109'
                boundary = {'space': 'segmented', 'segment': regs['segments']['cs'], 'offset': '0x0000FFFF'}
                saved = await call('memory.read', {**args, 'address': boundary, 'length': 1})
                await call('memory.write', {**args, 'address': boundary, 'data_base64': 'kA==', 'expected_sha256': saved['sha256']})
                last = await call('code.disassemble', {**args, 'address': boundary, 'instruction_count': 1})
                assert last['next_address']['offset'] == '0x00000000'
                await call('memory.write', {**args, 'address': boundary, 'data_base64': 'uA=='})
                failed = await call('code.disassemble', {**args, 'address': boundary, 'instruction_count': 1}, 'ADDRESS_NOT_MAPPED')
                assert failed['error']['data']['cause'] == 'segment_limit'
                await call('memory.write', {**args, 'address': boundary, 'data_base64': saved['data_base64']})
                regs = await call('state.get_registers', args)
                edit = {**args, 'registers': {'eax': '0x12345678'}, 'expected_state_revision': regs['state_revision'], 'request_id': 'register-once'}
                changed = await call('state.set_registers', edit)
                assert changed['general']['eax'] == '0x12345678'
                assert changed['state_revision'] == regs['state_revision'] + 1
                assert await call('state.set_registers', edit) == changed
                await call('state.set_registers', {**edit, 'registers': {'eax': '0x99999999'}}, 'REQUEST_ID_CONFLICT')
                await call('state.set_registers', {**args, 'registers': {'ebx': '0x00000000'}, 'expected_state_revision': regs['state_revision']}, 'STATE_REVISION_MISMATCH')
                await call('state.set_registers', {**args, 'registers': {'eax': '0x99999999', 'eflags': f"0x{int(changed['flags'], 16) ^ 131072:08X}"}, 'expected_state_revision': changed['state_revision']}, -32602)
                unchanged = await call('state.get_registers', args)
                assert unchanged == changed
                restored = await call('state.set_registers', {**args, 'registers': {'eax': regs['general']['eax']}, 'expected_state_revision': changed['state_revision']})
                flags = await call('state.set_registers', {**args, 'registers': {'eflags': f"0x{int(restored['flags'], 16) ^ 1:08X}"}, 'expected_state_revision': restored['state_revision']})
                assert int(flags['flags'], 16) == int(restored['flags'], 16) ^ 1
                restored = await call('state.set_registers', {**args, 'registers': {'eflags': restored['flags']}, 'expected_state_revision': flags['state_revision']})
                await call('code.disassemble', {**args, 'instruction_count': 257}, -32602)
                await call('state.set_registers', {**args, 'registers': {'eip': '0x00010000'}, 'expected_state_revision': restored['state_revision']}, 'ADDRESS_NOT_MAPPED')
                assert await call('state.get_registers', args) == restored
                bp_address = {'space': 'segmented', 'segment': regs['segments']['cs'], 'offset': '0x00000106'}
                bp = await call('breakpoints.create', {**args, 'kind': 'execution', 'address': bp_address, 'once': False})
                assert len((await call('breakpoints.list', args))['breakpoints']) == 1
                stopped = await wait(sid, await call('execution.continue', args))
                assert stopped['stop_reason']['kind'] == 'breakpoint'
                await call('breakpoints.delete', {**args, 'breakpoint_id': bp['breakpoint_id']})
                await call('execution.step', {**args, 'mode': 'into'})
                await call('execution.step', {**args, 'mode': 'over'})
                for space in ('linear', 'physical'):
                    at = {'space': space, 'offset': f"0x{(int(regs['segments']['cs'], 16) << 4) + 256:08X}"}
                    data = await call('memory.read', {**args, 'address': at, 'length': 6})
                    assert base64.b64decode(data['data_base64']) == bytes.fromhex('b83412bb7856')
                address = {'space': 'segmented', 'segment': regs['segments']['ds'], 'offset': '0x00000200'}
                before = await call('memory.read', {**args, 'address': address, 'length': 4})
                await call('memory.write', {**args, 'address': address, 'data_base64': '3q2+7w==', 'expected_sha256': '0' * 64}, 'MEMORY_PRECONDITION_FAILED')
                assert await call('memory.read', {**args, 'address': address, 'length': 4}) == before
                await call('memory.write', {**args, 'address': address, 'data_base64': '3q2+7w==', 'expected_sha256': before['sha256']})
                assert (await call('memory.read', {**args, 'address': address, 'length': 4}))['data_base64'] == '3q2+7w=='
                await call('breakpoints.create', {**args, 'kind': 'memory_change', 'address': address, 'once': False})
                await call('debugger.execute_command', {**args, 'command': 'CPU'})
                output = await call('debug.output.read', {**args, 'cursor': None, 'limit': 1})
                assert output['output']
                await call('trace.start', {**args, 'detail': 'normal', 'instruction_count': 2})
                await wait(sid, await call('execution.continue', args))
                page = await call('trace.read', {**args, 'cursor': None, 'limit': 1})
                assert len(page['events']) == 1
                if page['next_cursor']:
                    await call('trace.read', {**args, 'cursor': page['next_cursor'], 'limit': 1})
                await call('trace.stop', args)
                await stop(sid)
                entry = await start('AGENTFIX.COM')
                sid = entry['session_id']
                args = {'session_id': sid}
                segment = entry['stop_reason']['address']['segment']
                await call('breakpoints.create', {**args, 'kind': 'memory_change', 'address': {'space': 'segmented', 'segment': segment, 'offset': '0x00000200'}, 'once': False})
                hit = await wait(sid, await call('execution.continue', args))
                assert hit['stop_reason']['kind'] == 'breakpoint', hit
                await stop(sid)
                for command, expected_ip in (('AGCALL.COM', '0x00000103'), ('AGENTINT.COM', '0x00000102'), ('AGENTREP.COM', None)):
                    entry = await start(command)
                    sid = entry['session_id']
                    args = {'session_id': sid}
                    if command == 'AGENTREP.COM':
                        for _ in range(3):
                            await call('execution.step', {**args, 'mode': 'into'})
                        expected_ip = '0x0000010B'
                    step = await call('execution.step', {**args, 'mode': 'over'})
                    assert step['registers']['instruction_pointer'] == expected_ip, step
                    assert (await call('breakpoints.list', args))['breakpoints'] == []
                    await stop(sid)
                entry = await start('AGENTRUN.COM')
                sid = entry['session_id']
                args = {'session_id': sid}
                op = await call('execution.continue', args)
                started = time.monotonic()
                assert (await call('execution.wait', {**args, 'operation_id': op['operation_id'], 'timeout_ms': 1}))['running']
                assert time.monotonic() - started < 2
                await call('state.get_registers', args, 'TARGET_RUNNING')
                await call('code.disassemble', args, 'TARGET_RUNNING')
                await call('state.set_registers', {**args, 'registers': {'eax': '0x00000000'}, 'expected_state_revision': 1}, 'TARGET_RUNNING')
                await wait(sid, await call('execution.pause', args))
                await stop(sid)
                entry = await start('AGPMODE.COM')
                sid = entry['session_id']
                args = {'session_id': sid}
                await call('breakpoints.create', {**args, 'kind': 'execution', 'address': {**entry['stop_reason']['address'], 'offset': '0x00000191'}, 'once': False})
                await wait(sid, await call('execution.continue', args))
                regs = await call('state.get_registers', args)
                assert regs['cpu_mode'] == 'protected'
                await call('code.disassemble', {**args, 'instruction_count': 2})
                for segment, offset, cause in (('0x0020', '0x00000000', 'invalid_selector'), ('0x0008', '0x00010000', 'segment_limit')):
                    result = await call('code.disassemble', {**args, 'address': {'space': 'segmented', 'segment': segment, 'offset': offset}, 'instruction_count': 1}, 'ADDRESS_NOT_MAPPED')
                    assert result['error']['data']['cause'] == cause
                await call('state.set_registers', {**args, 'registers': {'eip': '0x00010000'}, 'expected_state_revision': regs['state_revision']}, 'ADDRESS_NOT_MAPPED')
                gdt_slot = {'space': 'physical', 'offset': f"0x{(int(entry['stop_reason']['address']['segment'], 16) << 4) + 432:08X}"}
                descriptor = await call('memory.read', {**args, 'address': gdt_slot, 'length': 8})
                await call('memory.write', {**args, 'address': gdt_slot, 'data_base64': base64.b64encode(bytes.fromhex('ffff0000009acf00')).decode(), 'expected_sha256': descriptor['sha256']})
                missing = await call('code.disassemble', {**args, 'address': {'space': 'segmented', 'segment': '0x0018', 'offset': '0x00400000'}, 'instruction_count': 1}, 'ADDRESS_NOT_MAPPED')
                assert missing['error']['data']['cause'] == 'page_not_present', missing
                await stop(sid)
                entry = await start('AGENTFIX.COM')
                await stop(entry['session_id'])
        assert backend.process.poll() is None, 'Client disconnect stopped emulator'
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 25)
        listener.close()
        assert backend.process is None or backend.process.poll() is not None, 'Owned emulator leaked'
        path = config.parent / ('mcp-noheavy-results.json' if no_heavy else 'mcp-e2e-results.json')
        path.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(f"MCP fixture acceptance passed ({len(evidence)} recorded operations).")
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--no-heavy', action='store_true')
    parser.add_argument('--port', type=int, default=18765)
    args = parser.parse_args()
    asyncio.run(run(args.config.resolve(), args.port, args.no_heavy))
