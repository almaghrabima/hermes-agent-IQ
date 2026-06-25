# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

fast-rlm is an implementation of **Recursive Language Models (RLMs)**: an LLM interacts with an arbitrarily long prompt through an external Python REPL instead of loading it all into context. The agent writes code to explore/slice/transform the prompt, and can recursively spawn sub-agents whose results return as REPL variables (never auto-loaded into the parent's context).

It ships as a **Python package** (`fast_rlm`, on PyPI) that is a thin shim over a **Deno/TypeScript engine** (`src/`). All the actual RLM logic lives in the TS engine; the Python side only marshals arguments and launches a subprocess.

## The two-language split (most important architecture fact)

- **Python (`fast_rlm/`)** — public API. `run()` / `RLMConfig` (`_runner.py`) and the `fast-rlm` CLI (`_cli.py`). `run()` validates config, serializes everything (query, schema, tools, env, MCP config, llm_kwargs) into **temp JSON/YAML files**, builds a `deno run ...` command with the minimal `--allow-*` flags the run needs, pipes the query in via **stdin**, and reads results back from a temp `--output` JSON file. Python has essentially no RLM logic.
- **TypeScript engine (`src/`)** — runs under Deno. `subagents.ts` is the heart: the recursive agent loop. It loads **Pyodide** (Python compiled to WASM) and runs the agent's generated code inside that sandboxed REPL.
- **Pyodide REPL** — where the *agent's* generated Python actually executes. It is pure WASM: no subprocess, no sockets, no filesystem. `requests`/`httpx` are patched to route through the Deno host's fetch. Anything the REPL needs from the outside world (sub-agent calls, MCP tools) is reached via JS bridge functions injected as Python globals (`__js_llm_query__`, `__js_mcp_call__`, etc.).

Engine discovery (`_find_engine_dir`): a pip install bundles `src/` into `fast_rlm/_engine/` (see `pyproject.toml` `force-include`); a dev/editable checkout walks up to the repo root. When editing the engine in a checkout, changes are picked up immediately — no rebuild.

## The recursive loop (`src/subagents.ts`)

`subagent(context, depth, ...)` is called once for the root (depth 0) and recursively for every sub-agent. Each invocation:
1. Boots a fresh Pyodide instance and injects setup code defining `context`, `FINAL(x)`, `llm_query`, `batch_llm_query`, MCP proxies, tool registration, and a JSON pretty-printing `print` override.
2. Runs a **step-0 probe** that prints the shape of `context` (dict keys + previews, or string length + head/tail) plus available tools/MCP — this becomes the first user message.
3. Loops up to `max_calls_per_subagent`: ask the model for a ```repl``` block → execute it in Pyodide → truncate stdout to `truncate_len` (last N chars) → feed back as the next user message. The loop ends when the agent calls `FINAL(...)`.
4. If an `output_schema` is set, the `FINAL` value is validated with **ajv**; on failure the schema + errors are fed back and the agent retries within its remaining budget.

Sub-agents are spawned when the agent's REPL code calls `await llm_query(ctx, schema=..., tools=..., mcp=..., instruction=...)`. This routes through `__js_llm_query__` → a recursive `subagent(...)` call at `depth+1`. `batch_llm_query(...)` is the parallel form (a drop-in for `asyncio.gather` over `llm_query` calls) with a single shared compression check; calling `llm_query` handles directly inside `asyncio.gather` is blocked by a guard that redirects you to `batch_llm_query`.

**Inheritance rules (intentional, easy to get wrong):** sub-agents do NOT inherit the parent's tools, MCP servers, or instruction. The parent must re-grant each explicitly per `llm_query` call. `env_variables` and `llm_kwargs` DO propagate to all descendants.

### Compression guard

A correctness/cost feature (`enable_compression_guard`, default on). When an agent delegates a large, barely-compressed slice of its own context (`childChars >= compression_ratio * parentChars` and `parentChars >= compression_min_chars`), the child must **self-confirm** (a YES/NO LLM call with the same model) before running; a NO throws `DELEGATION_REJECTED` and forces the caller to slice/summarize in its own REPL first. `batch_llm_query` does this once for the whole fan-out. Implemented via `confirmDelegation` in `call_llm.ts`.

## Executors (Phase 1 + Phase 2)

fast-rlm has two executors for the agent's generated Python code. The executor is selected with the `executor` config key in `rlm_config.yaml` / `RLMConfig` / the temp config file, and defaults to `"pyodide"`.

### `pyodide` (default)

Runs the agent's code in an in-process WASM VM (Pyodide loaded into the Deno engine). Fully sandboxed: no real sockets, no filesystem access, no subprocess. `requests`/`httpx` are patched to route through Deno's fetch. Only pure-Python wheels can be installed (no native extensions — no pandas, no numpy, no C-backed libraries). This is the safe choice for any multi-tenant or untrusted-agent scenario.

### `subprocess`

Spawns an out-of-process **native-Python kernel** (`python_kernel/kernel.py`) that the Deno engine drives over a control channel. The kernel is a persistent native-Python REPL: full Python — pandas, numpy, and any package installed in the target interpreter all work. The Deno engine side (`src/kernel_client.ts`) owns the lifecycle: it spawns the kernel process, forwards `run_step` requests, and routes mid-step callbacks (`llm_query`, `batch_confirm`, `mcp_call`, `mcp_read_resource`) back to the same host handlers the pyodide path uses, so fast-rlm's recursive sub-agent machinery works identically either way.

The `subprocess` executor has two **sandbox modes**, selected by `kernel_sandbox`:

#### `kernel_sandbox: local` (Phase 1 — un-sandboxed)

The kernel runs as a bare native subprocess on the host over a UNIX socket (TCP loopback on Windows). It has full access to the host — real network, real filesystem, real subprocess. This mode requires `executor_unsandboxed_ack: true` to start. Do **not** use it for untrusted agent code in production.

> **Security caveat (`kernel_sandbox: local`):** The kernel process is UN-SANDBOXED. Treat this mode as trusted-input-only.

The interpreter used by the kernel is chosen by the `RLM_KERNEL_PYTHON` environment variable (default: `python3`). Point it at any Python that has the libraries the agent code needs.

#### `kernel_sandbox: docker` (Phase 2 — container-sandboxed)

The kernel runs **inside a Docker container**, driven over a **stdio control channel** (the kernel's `--stdio` mode). No `executor_unsandboxed_ack` is required because the kernel is sandboxed inside the container. The Deno side uses `ProcStdioTransport` to communicate with the kernel child process.

The effective `docker run` command is:

```
docker run --rm -i [--runtime <kernel_runtime>] --network <kernel_network> \
  -v <kernel.py>:/kernel.py:ro <kernel_image> python /kernel.py --stdio
```

`kernel.py` is bind-mounted into the container read-only — no image rebuild is needed when the kernel source changes.

**Sub-keys for `docker` mode:**

| Key | Default | Notes |
|---|---|---|
| `kernel_runtime` | *(Docker default — `runc`)* | Set to `runsc` for **gVisor** syscall-level isolation. **Linux hosts with gVisor only** — not available on macOS/Docker Desktop. Selecting `runsc` on a host without gVisor fails with Docker's "unknown runtime" error. |
| `kernel_image` | `python:3.11-slim` | The container image. The kernel itself is stdlib-only and works with any Python 3.8+ image. **The agent's code runs here too**, so use a richer image (e.g. one with pandas/numpy pre-installed) if the agent needs those libraries. |
| `kernel_network` | `none` | The Docker network mode. Default `none` means the agent's code has **no network egress**. `llm_query`/MCP calls still work because they ride the stdio control channel to the host — the host makes the LLM/MCP calls, not the container. Set to `bridge` if the agent code itself must make direct HTTP requests. |

**Boundary strength is host-OS-dependent.** True VM-level isolation (gVisor's KVM
platform, or the proposed Firecracker/Kata microVM runtimes) requires a **Linux host
with `/dev/kvm`** — it is *not* native on macOS or Windows. Practical guidance:

| Host OS | What `docker` mode actually gives you today | Strongest available boundary |
|---|---|---|
| **Linux** | `runc` (namespaces+cgroups) or `runsc` (gVisor user-space syscall interception) | gVisor today; **Firecracker/Kata microVM proposed** (Linux+KVM only) |
| **macOS** | `runc` only, **inside Docker Desktop's shared Linux VM** (no `runsc`, no per-exec microVM) | the host Linux VM itself; native Seatbelt is weaker and repeatedly bypassed |
| **Windows** | `runc` via the WSL2/Docker-Desktop Linux VM | Hyper-V isolation, or run the Linux `runc`/`runsc` stack under WSL2 |

Only `runc` and `runsc` are implemented; `runsc` is Linux-only and fails with Docker's
"unknown runtime" error elsewhere. The per-OS matrix and the proposed microVM backend
are specified in the Hermes repo at
`docs/rlm/2026-06-25-fast-rlm-kernel-phase4-per-os-boundaries-design.md`.

### Config reference

| Key | Type | Default | Notes |
|---|---|---|---|
| `executor` | `"pyodide" \| "subprocess"` | `"pyodide"` | Selects the code executor |
| `executor_unsandboxed_ack` | `bool` | `false` | Must be `true` to start `subprocess` with `kernel_sandbox: local` |
| `kernel_sandbox` | `"local" \| "docker"` | `"local"` | Sandbox mode for the `subprocess` executor |
| `kernel_runtime` | `str` | *(runc)* | Docker runtime — `runsc` for gVisor (Linux + gVisor only; fails elsewhere); `docker` mode only. Boundary strength is host-OS-dependent (see "Boundary strength" above) |
| `kernel_image` | `str` | `python:3.11-slim` | Container image for the kernel; `docker` mode only |
| `kernel_network` | `str` | `none` | Docker network mode; `docker` mode only |
| `RLM_KERNEL_PYTHON` (env) | path/name | `python3` | Python interpreter for `kernel_sandbox: local` |

The `subprocess` executor also requires the Deno process to have `--allow-run` (to spawn the kernel or `docker run`) and `--allow-write` (for the UNIX socket in local mode). `_runner.py` grants `--allow-run` automatically when `executor: subprocess` is configured, covering both local and docker sandbox modes.

## Backends (`src/call_llm.ts` dispatch)

The `primary_agent`/`sub_agent` string selects the backend by prefix. `primary_agent` is **required and has no default** — `run()` and the engine both raise if it is unset; `sub_agent` falls back to `primary_agent`.

| Selector | Module | Credential |
|---|---|---|
| unprefixed (`gpt-5-mini`, `z-ai/glm-5`) | OpenAI-compatible (`call_llm.ts`) | `RLM_MODEL_API_KEY` → `OPENAI_API_KEY` → `OPENROUTER_API_KEY` (+ `RLM_MODEL_BASE_URL`, default OpenRouter) |
| `vertex/…` (or `RLM_VERTEX_AI=1` / `run(vertex=True)`) | `vertex.ts` | gcloud ADC + `GOOGLE_CLOUD_PROJECT` |
| `claude-…` / `anthropic/…` | `anthropic.ts` (native SDK), **falls back** to OpenAI-compatible if no `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` (or `RLM_ANTHROPIC_API_KEY`) |
| `acp:…` (`acp:claude-code`, `acp:codex`, `acp:opencode`) | `acp.ts` | none — drives a local coding agent read-only via its own CLI login |

ACP agents are spawned as child processes, run read-only in a throwaway temp cwd, and have **zero token/cost usage** — so token/money budgets don't bite. For that reason ACP runs default `max_global_calls` to 50; set it explicitly for any ACP run.

## Budgets

Enforced in the loop in `subagents.ts`, tracked globally in `usage.ts` across all agents/backends: `max_money_spent`, `max_completion_tokens`, `max_prompt_tokens`, and `max_global_calls` (the only one that works for ACP, where usage is always zero). Exceeding any throws and ends the run.

## MCP (`src/mcp.ts`)

The MCP client lives **host-side in Deno**, not in the WASM REPL. One connection pool is opened at process start and shared across all agents. The REPL reaches tools through Python proxies (`mcp_call`, `mcp_list_tools`, `mcp_read_resource`, …) bridging to `__js_mcp_call__`. `mcp.ts` and its heavy SDK dependency are **lazy-imported** only when a run configures MCP servers — non-MCP runs never load it. Servers are scoped: the root sees all; sub-agents see only what the parent grants via `llm_query(..., mcp=[names])`.

## Common commands

```bash
# --- Dev setup (from-source) ---
uv pip install -e .                      # editable Python install
cd tui_log_viewer && bun install && cd ..  # log-viewer deps (one-time)

# --- Run the engine directly (Deno tasks defined in deno.json) ---
echo "What is 2+2?" | deno task subagent --config rlm_config.yaml --output /tmp/out.json
deno task test_counting_r                # end-to-end smoke (uses test_counting_r.config.yaml)

# --- Run via the Python package / CLI ---
fast-rlm "Generate 50 fruits and count number of r" --primary-agent z-ai/glm-5
fast-rlm "Aggregate the reviews" --input-file reviews.json --primary-agent z-ai/glm-5 -q
python examples/structured_io.py         # examples/ are runnable end-to-end demos

# --- Tests (Deno) ---
deno test --allow-read --allow-env tests/instruction_test.ts        # pure unit test
deno test --allow-read --allow-env --allow-net --allow-write tests/repl_calls_test.ts  # real Pyodide
deno test --allow-read --allow-env --allow-net --allow-write tests/  # all

# --- Benchmarks (need the extra) ---
uv sync --extra benchmarks
uv run benchmarks/longbench_benchmark.py

# --- Logs ---
./viewlog logs/<file>.jsonl              # TUI viewer (needs bun)
fast-rlm-log logs/<file>.jsonl --stats   # or --tui
deno task view_logs
```

Note: there is no lint/format config beyond Deno's built-ins; use `deno fmt` / `deno lint` on TS, standard tooling on Python. The repo has no pytest suite — `tests/` are Deno tests; `examples/` and `benchmarks/` are the integration-level checks (they hit real LLM APIs and cost money).

## Configuration

`rlm_config.yaml` at the repo root holds the engine defaults; `RLMConfig` (Python) and CLI flags override it. The merge order in `run()` is: `rlm_config.yaml` defaults → user `config` overrides → required-field validation. The same fields are documented inline in both `rlm_config.yaml` and the `RLMConfig` dataclass in `_runner.py` (keep them in sync when adding a config knob — it must be threaded through `_runner.py` → temp config file → `config.ts` → `subagents.ts`).

Key ablation toggles (`enable_tools`, `enable_structured_io`, `enable_compression_guard`): when false, the capability is removed from the REPL **and** stripped from the system prompt (`prompt.ts` `buildSystemPrompt` takes `PromptOptions`).

## Logging

`logging.ts` writes structured JSONL via Pino to `logs/` (one line per step/event, including code, output, reasoning, usage, timestamps). `ui.ts` renders the live terminal view (spinners, step boxes). The `tui_log_viewer/` (OpenTUI + Bun, React/TSX) replays a log file interactively.
