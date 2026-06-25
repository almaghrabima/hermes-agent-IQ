# python_kernel — native-Python REPL kernel

This directory contains the out-of-process Python kernel used by the `subprocess` executor. It is a stdlib-only, persistent native-Python REPL that replaces Pyodide when native libraries (pandas, numpy, etc.) are needed. One kernel process is spawned per sub-agent; the namespace `G` (a plain `dict` used as the exec globals) persists across all steps within that sub-agent's lifetime.

## Files

| File | Purpose |
|---|---|
| `kernel.py` | The kernel itself — self-contained, stdlib only |
| `kerneltest.py` | Standalone stdlib test driver (no Deno required) |

## Wire protocol

The host (Deno, `src/kernel_client.ts`) and kernel communicate over a UNIX socket (TCP loopback on Windows) using **length-prefixed JSON frames**:

```
[ 4-byte big-endian uint32: payload length ][ UTF-8 JSON payload ]
```

Every frame is a JSON object with at least `{kind, op, id}`:

- `kind`: `"req"` or `"resp"`
- `op`: the operation name (see below)
- `id`: a positive integer used to correlate responses to requests

**ID parity convention:** the host owns **even** ids; the kernel owns **odd** ids. Both sides send both requests and responses — for example, the host sends a `run_step` request, and mid-execution the kernel sends `llm_query` requests back to the host, whose responses the host sends back on the same socket.

## Operations

### Host → kernel

| Op | Direction | Extra fields | Response fields |
|---|---|---|---|
| `setup` | host → kernel | `code: str` (setup script to exec into `G`) | — |
| `register_tool` | host → kernel | `src: str` (tool function source) | — |
| `run_step` | host → kernel | `code: str` (agent step code) | `stdout`, `error`, `final_set`, `final_value`, `final_error` |
| `reset_final` | host → kernel | — | — |
| `shutdown` | host → kernel | — | — (kernel exits) |

`run_step` compiles the agent's code with `PyCF_ALLOW_TOP_LEVEL_ALLOW_AWAIT` so that top-level `await llm_query(...)` works, then execs it into `G` so variables persist across steps. stdout/stderr are captured via redirect; the host applies its own `truncate_len` to the returned `stdout`.

### Kernel → host (callbacks during `run_step`)

| Op | When used |
|---|---|
| `llm_query` | Agent called `await llm_query(...)` — recursive sub-agent delegation |
| `batch_confirm` | Compression-guard self-confirmation check |
| `mcp_call` | Agent called an MCP tool |
| `mcp_read_resource` | Agent read an MCP resource |

The kernel pre-defines `__js_llm_query__`, `__js_batch_confirm__`, `__js_mcp_call__`, and `__js_mcp_read_resource__` in `G` as async shims over a shared `__host_call__` coroutine. This means fast-rlm's existing `setup` code (which uses those same names) runs unchanged on either executor path.

## JSON-only boundary

All values that cross the socket must be JSON-serializable: `llm_query` context and result, the `FINAL(x)` value, and MCP call arguments/results. If the value passed to `FINAL(x)` is not JSON-serializable, the kernel returns `final_error` instead of `final_value` (rather than silently sending a proxy object). Agent code should ensure `FINAL` receives dicts, lists, strings, numbers, or `None`.

## Concurrency and the deadlock-free `serve()` loop

`serve()` dispatches incoming frames via `asyncio.ensure_future` (fire-and-forget) rather than `await`-ing them inline. This is intentional: a `run_step` handler must be able to `await __host_call__(...)` (which sends a request frame and waits for a response), while the event loop concurrently reads the host's response frame. If `serve()` awaited handlers inline it would block the read loop, the response would never arrive, and the `run_step` would deadlock.

```
serve() loop
  └─ read frame → asyncio.ensure_future(handle(frame))   # non-blocking dispatch
       └─ handle run_step
            └─ await __host_call__("llm_query", ...)
                 ├─ sends llm_query request frame
                 └─ awaits response             ← serve() loop reads it concurrently
```

## Running tests

```bash
# Stdlib driver — no Deno, no network, covers protocol + exec semantics
python3 python_kernel/kerneltest.py

# Integration test — requires Deno + --allow-net/run/write
deno test tests/kernel_client_test.ts
```

## Stdlib-only constraint

`kernel.py` imports only Python standard library modules. This keeps the kernel compatible with any Python 3.8+ interpreter without requiring a pip install. The agent's own code (running inside `G`) may import anything that is installed in the interpreter pointed to by `RLM_KERNEL_PYTHON`.
