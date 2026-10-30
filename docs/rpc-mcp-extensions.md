# RPC extensions for MCP

These additive methods retain JSON-RPC protocol version 1.0 and the existing named-pipe transport, controlled mount policy, session state machine, and request-ID completion cache. They extend the earlier [RPC baseline](rpc-development.md). The older TCP command bridge is unchanged.

## `state.set_registers`

```json
{
  "jsonrpc": "2.0",
  "id": "edit-1",
  "method": "state.set_registers",
  "params": {
    "session_id": "ses-1",
    "expected_state_revision": 1,
    "registers": {"eax": "0x12345678", "eflags": "0x00007203"}
  }
}
```

`registers` is a nonempty object whose supported names are `eax`, `ebx`, `ecx`, `edx`, `esi`, `edi`, `ebp`, `esp`, `eip`, and `eflags`. Names are lowercase; values are `0x` followed by exactly eight hexadecimal digits. The expected revision is an unsigned 64-bit integer.

The target must be stopped. Validation checks every name and value, the revision at emulation-thread execution time, EFLAGS restrictions, and EIP against the current code segment and instruction mode before changing any register. The complete edit applies on the emulation thread and increments the session revision exactly once. Its result has the same shape as `state.get_registers`, including the new revision. A rejected request changes neither registers nor revision.

EFLAGS is a complete value: bits outside CF/PF/AF/ZF/SF/TF/IF/DF/OF must match the original snapshot. The example flag value is illustrative; callers must calculate it from their own snapshot. Segment, control, and FPU registers are not editable.

Malformed requests and protected flag changes return `-32602`. An expected revision mismatch returns `-32018` with `data.reason="STATE_REVISION_MISMATCH"` and current session/revision details. EIP outside the current code segment/mode returns `ADDRESS_NOT_MAPPED` with cause `segment_limit`. Running targets return `TARGET_RUNNING`.

The queued transaction stores its final response and revision at commit time. If the request times out before the queue finishes, explicit resubmission with the same ID and payload waits for that transaction rather than applying it again. Conflicting payloads with the same request ID return `REQUEST_ID_CONFLICT`. The MCP adapter never automatically resubmits mutations.

## `code.disassemble`

```json
{
  "jsonrpc": "2.0",
  "id": "disasm-1",
  "method": "code.disassemble",
  "params": {
    "session_id": "ses-1",
    "address": {"space": "segmented", "segment": "0x0814", "offset": "0x00000100"},
    "instruction_count": 16
  }
}
```

`address` is optional and defaults to current CS:EIP. Only segmented addresses are accepted. `instruction_count` defaults to 16 and must be 1–256. The target must be stopped.

The response contains `session_id`, `state_revision`, an `instructions` array, and segmented `next_address`. Each instruction contains `address`, `data_base64`, `length`, and native disassembly `text`. It does not mutate guest state or increment the revision.

Mode-aware checked memory resolution supplies bytes incrementally to the existing native decoder. This avoids speculative reads beyond the instruction or substitution of synthetic guest bytes at inaccessible boundaries. Current CS uses its cached segment base and mode; other protected-mode addresses require a present code descriptor. A single instruction is limited to 15 bytes. A complete 16-bit instruction may wrap the following IP; an instruction truncated by its segment boundary fails explicitly.

Failures return the existing `ADDRESS_NOT_MAPPED` code with address details and causes such as `invalid_selector`, `segment_limit`, `page_not_present`, or `instruction_too_long`. A failed request returns no partial success page. Invalid counts/address spaces return `-32602`.

## Capabilities and lifecycle

`agent.capabilities` additionally returns:

```json
{
  "state": {"set_registers": true},
  "code": {"disassemble": true},
  "ready": true,
  "limits": {"max_disassembly_instructions": 256}
}
```

Existing capability fields remain. `ready` is checked on the emulation-thread queue and indicates that the shell is ready to start a new target. An active session can make it false. The adapter requires a backend built with these extensions and waits for readiness before reporting startup success.

Session termination removes session-owned native breakpoints and stops native tracing before DOS cleanup. Forced termination of a protected-mode target restores the captured shell addressing context through existing CPU APIs before PSP cleanup. This enables subsequent real-mode sessions without changing CPU execution implementations or memory mapping semantics.

Python clients expose `set_registers` returning `RegisterSnapshot` and `disassemble` returning `Disassembly`; instruction bytes are Python `bytes`. MCP tools use base64 for bytes. See the [English workflow and tool reference](mcp-agent-guide.md).
