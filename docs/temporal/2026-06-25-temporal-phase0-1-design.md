# Design: Temporal durable orchestration (Phase 0 + Phase 1)

**Date:** 2026-06-25
**Status:** Approved (design)
**Part of:** "Temporal for better agents" — a 5-phase effort (P0 foundation, P1 reliable
orchestration, P2 crash-durable background agents, P3 human-in-the-loop, P4 cron/kanban
backend swap). This spec covers **P0 + P1 only**.

## Context

Hermes already has orchestration/durability primitives, but with a known gap:

- `cron/` — durable scheduled jobs (persisted, locked, `resume_job`).
- `delegate_task` — subagents, but **process-local**: AGENTS.md states background
  delegation does *not* survive a process restart.
- `kanban` — persistent multi-agent work queue.
- The agent loop (`AIAgent.run_conversation`) is **synchronous and in-process** — no
  durable execution, no automatic retry/timeout around flaky steps.

[Temporal](https://learn.temporal.io/) is a durable-execution engine: workflows and
their activities survive process crashes, retry with backoff, enforce timeouts, and
give exactly-once activity semantics. This effort introduces it **at the edges** (an
opt-in plugin + external service), respecting Hermes's two invariants:

1. **Prompt caching is sacred** — the synchronous agent loop and its byte-stable system
   prompt are NOT touched. Durable work is started via an explicit tool, out-of-band.
2. **Narrow-waist core** — Temporal is a `check_fn`-gated, opt-in plugin tool, never a
   core tool. It follows the Footprint Ladder (plugin + external service).

### Goals (P0 + P1)
- Land the Temporal integration rails (client, worker, config, packaging, gating).
- Give agents a way to run **reliable multi-step jobs** with retries/timeouts/backoff
  and exactly-once activity semantics, via a new opt-in tool.
- Support **both** deployment targets — Temporal Cloud and self-hosted — with the
  local **dev-server as the default**.

### Non-goals (deferred to later phases)
- P2: wrapping background `delegate_task` for restart-resume.
- P3: signals / human-in-the-loop / long waits.
- P4: re-platforming `cron` / `kanban` onto Temporal.
- Transparent wrapping of every tool call in the agent loop (rejected — invasive to
  the loop and risks the caching invariant).

## Architecture

The agent process holds only a lightweight Temporal **client** (start/query workflows).
The **worker** that executes workflows + activities runs as a separate process
(`hermes temporal worker`). Separation keeps the agent process light and the
synchronous loop untouched.

```
agent process                         worker process (hermes temporal worker)
  durable_run tool ──start_workflow──▶ Temporal server ──polls──▶ DurableRunWorkflow
  durable_status  ──query/result────▶   (dev-server          │ step → run_step_activity
                                          or Cloud)           │   (RetryPolicy, timeout)
                                                              └ subagent executes in
                                                                Hermes context
```

## Components

| File | Responsibility |
|---|---|
| `plugins/temporal/__init__.py` | Plugin entry: register config schema, lazy dep, `check_fn`, tools, CLI command. |
| `plugins/temporal/client.py` | `get_client()` — connect using resolved config (dev vs Cloud, tls, namespace); lazy-imports `temporalio`. |
| `plugins/temporal/workflows.py` | `DurableRunWorkflow` — orchestrates an ordered list of steps as activities. |
| `plugins/temporal/activities.py` | `run_step_activity(step)` — executes one step (a subagent task, reusing delegation machinery) inside Hermes context. |
| `plugins/temporal/worker.py` | Worker bootstrap + `hermes temporal worker` subcommand; optional dev-server auto-start. |
| `tools/` (registered by the plugin) | `durable_run`, `durable_status` — service-gated via `check_fn`. |

Packaging: `[project.optional-dependencies] temporal = ["temporalio==X.Y.Z"]` — exact-
pinned to the latest stable `temporalio` resolved at implementation time (the
implementation plan pins the concrete version), NOT in `[all]`, plus a
`tools/lazy_deps.py` entry so first use can lazy-install. Install:
`uv pip install -e ".[temporal]"`.

## Configuration

`config.yaml` (behavioral settings; never secrets, never new `HERMES_*` env vars):

```yaml
temporal:
  enabled: false               # master switch; tools gated off unless true
  target: "localhost:7233"     # dev-server default; Cloud: "<ns>.<acct>.tmprl.cloud:7233"
  namespace: "default"
  tls: false                   # Cloud → true
  task_queue: "hermes"
  dev_server: true             # auto-start `temporal server start-dev` when target unreachable (dev only)
  step_timeout_seconds: 600    # default per-activity timeout
  default_retry:               # default activity RetryPolicy
    max_attempts: 3
    initial_interval_seconds: 1
    backoff_coefficient: 2.0
```

`.env` (secrets only): `TEMPORAL_API_KEY` (Cloud API-key auth) **or** mTLS cert/key
paths (`TEMPORAL_TLS_CERT`, `TEMPORAL_TLS_KEY`). Integrate with `hermes tools` and
`hermes setup` like other service-gated features; never a raw env var for non-secret
config.

## Service gating (`check_fn`)

`durable_run` / `durable_status` are present **only** when `temporal.enabled` is true
AND a client can connect (or `dev_server` can start one). When ungated-off, the tools
never enter the tool catalog — so there is no broken tool on the API surface, preserving
the narrow waist and avoiding wasted tokens.

## Tool surface

### `durable_run`
```
durable_run(
  steps: [ { "name": str, "prompt": str, "sub_agent"?: str } ],   # ordered subagent steps
  retry?: { "max_attempts": int, "backoff_coefficient": float, "initial_interval_seconds": int },
  step_timeout_seconds?: int,
  wait_seconds?: int = 30,    # how long to block for an inline result before returning a handle
)
```
Behavior: resolve client → `start_workflow(DurableRunWorkflow, {steps, retry, ...},
id=<run_id>, task_queue=...)`. Block up to `wait_seconds` for completion; if it finishes,
return `{status:"completed", run_id, result}`; otherwise return
`{status:"running", run_id}` for later polling. Returns `{status:"error", error}` if the
service is unreachable in a way `check_fn` didn't catch.

### `durable_status`
```
durable_status(run_id: str)
```
Returns `{status: "running"|"completed"|"failed"|"pending", run_id, result?, error?}`.
`pending` specifically covers "workflow accepted but no worker is polling the task
queue" — surfaced distinctly so the operator knows to start `hermes temporal worker`.

## Data flow

1. Agent calls `durable_run(steps=[...], retry=...)`.
2. Tool gets the client and starts `DurableRunWorkflow` on `task_queue`, returns/holds a `run_id`.
3. The separate worker picks up the workflow; for each step it schedules
   `run_step_activity` with the resolved RetryPolicy + timeout.
4. `run_step_activity` runs one subagent task in Hermes context (reusing the
   delegation execution path), returns the step result.
5. Workflow aggregates step results; on completion the result is available via the
   started handle and `durable_status`.
6. Long runs re-enter the conversation later when the agent calls `durable_status`
   (same async pattern as background delegation).

## Error handling

- **Server unreachable + `dev_server: true`** → auto-start `temporal server start-dev`
  (dev only; never in a packaged/Cloud config). Else `check_fn` gates the tools off.
- **Activity failure** → Temporal RetryPolicy (max attempts, exponential backoff).
  Errors marked non-retryable bypass retries and surface to the workflow result.
- **`durable_run` wait_seconds exceeded** → return `run_id` with `status:"running"`;
  the workflow keeps executing durably.
- **No worker polling** → workflow stays queued; `durable_status` reports `pending`
  with a hint to start the worker.
- **`temporalio` not installed** → lazy-install attempt; on failure, a clear message
  naming the `[temporal]` extra (mirrors the rlm "engine_path/kernel support" guard).

## Testing

Per repo conventions: temp `HERMES_HOME`, no real Cloud, no change-detector tests.

- **Unit (no server needed):**
  - Config resolution: dev-server defaults, Cloud (tls + target + auth), disabled.
  - `check_fn` gating: tools absent when `enabled:false` / unreachable; present when reachable (connection mocked at the boundary).
  - Tool arg validation for `durable_run` / `durable_status`.
  - Lazy-dep guard: clear error naming the `[temporal]` extra when `temporalio` is missing.
- **Integration (gated; skips when no `temporal` binary — same gating style as the rlm docker/KVM e2e):**
  - Against `temporal server start-dev`: submit a workflow with a deliberately flaky
    activity (fails N-1 times) and assert it **retries → completes** with exactly-once
    effect; assert `durable_status` returns the final result; assert a non-retryable
    error surfaces as `failed`.

## Footprint Ladder justification

New capability lands as **plugin + external service** (rung 4–5), not a core tool
(rung 6). It reuses existing machinery (delegation execution path for steps; config/
lazy-dep/check_fn patterns already used by other service-gated tools), touches neither
the agent loop nor the system prompt, and is fully removable by disabling the plugin.
