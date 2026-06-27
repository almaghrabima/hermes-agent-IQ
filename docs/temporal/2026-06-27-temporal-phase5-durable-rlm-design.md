# Design: Temporal Phase 5 — durable background rlm

**Date:** 2026-06-27
**Status:** Approved (design)
**Part of:** "Temporal for better agents." This is **Phase 5** — durable background
execution for the `rlm` (fast-rlm / Recursive Language Model) tool. Builds on
Phase 0+1 (PR #17: plugin, worker, config, gating), reuses the Phase-2/3 durable
**outbox → completion-drain → reconcile** delivery rail (PRs #20/#24/#23), and the
Phase-0 `hermes temporal worker`.

## Context

`rlm` runs a Recursive Language Model over long context via fast-rlm. The agent
calls the `rlm` tool (`tools/rlm_tool.py`, `RLM_SCHEMA`); the handler resolves
config + credentials and invokes `_run_rlm_in_env(env, env_type, …)`, which stages
a driver + config into the active Hermes backend and runs a **single blocking
Deno subprocess** (`deno run src/subagents.ts`) that drives a stateful kernel REPL.
The whole invocation returns **one JSON result** (`{status, result, usage,
log_path}`), with a default `timeout_seconds: 600`.

Two properties matter for durability:
1. **The run is already one opaque blocking unit.** From Hermes's side an rlm call
   is request → (up to 600s) → single result. There is no streaming/partial API.
2. **Kernel state is live, non-serializable Python.** The kernel's `G` namespace
   persists across steps *within one run* but has no checkpoint/serialize
   mechanism; on a process crash the kernel dies and all state is lost.

Today an rlm call is **synchronous and ephemeral**: if the host/gateway process
dies mid-run, the result is lost and nothing re-runs it. There is no background
mode — the caller blocks until the result (or timeout).

### Invariants
1. **Opt-in, zero regression.** The synchronous `rlm` path is untouched; durable
   mode is reached only via `rlm(durable=true)` and gated on `temporal.enabled`.
2. **Narrow waist.** No new core tool — one new parameter on the existing `rlm`
   tool, plus a workflow/activity in the existing temporal plugin. Reuses the
   Phase-2/3 delivery rail wholesale.
3. **No silent fallback.** `durable=true` without `temporal.enabled` is an error
   (mirrors `delegate_task durable`); the plain `rlm` call remains available.

## Goals

- `rlm(durable=true)` dispatches an **`RlmRunWorkflow`** on the `hermes temporal
  worker` and returns a `run_id` immediately (background).
- The run **survives a host/gateway crash**: Temporal re-runs the whole rlm
  invocation from scratch (bounded), executing it in the always-on worker.
- The completed result **re-enters the originating session** via the existing
  durable outbox + completion-drain rail (same as durable delegation), and is
  pollable via `durable_status`.
- Startup **reconcile** backfills results completed while the consumer was down.

## Non-goals (Phase 5)

- **Mid-run / step-level checkpoint-resume.** We do NOT serialize the kernel `G`
  namespace, suspend/resume the Deno bridge, or replay steps. A crashed run is
  re-run from scratch. (Considered and rejected: very high cost, and lossy/
  unreliable for non-deterministic LLM-driven runs.)
- **Durable mode over remote Hermes backends** (ssh/modal/daytona). The activity
  runs rlm with the **local backend on the worker host** (see Limitations). rlm's
  docker `kernel_sandbox` already requires the local backend, so this matches
  existing constraints.
- **rlm-as-cron / rlm-as-kanban-worker.** Scheduling or board-driven rlm are
  separate, later concerns.

## Architecture

```
rlm(query=…, durable=true)
  ├─ gate: temporal.enabled (else error — NO silent fallback)
  ├─ dispatch_durable_rlm(rlm_args, session_key)
  └─ returns {status: "dispatched", run_id}    ← caller keeps working
                                   │
            hermes temporal worker ┘
              RlmRunWorkflow(payload)
                 retry_policy: maximum_attempts = rlm.durable_max_attempts (default 2)
                 start_to_close_timeout = rlm.timeout_seconds + buffer
                 └ activity: run_rlm_durable(payload)
                     ├ discover_builtin_tools()  (worker already bootstraps this)
                     ├ runs the SAME rlm invocation (_run_rlm_in_env, local backend, worker host)
                     └ records result/failure block → durable outbox (keyed by session_key)
                                   │
        outbox drain (Phase 2/3 rail) ┘──▶ result re-enters the originating session as a completion
                                          (queryable via durable_status; startup reconcile backfills)
```

### Components (mostly reuse)

| File | Responsibility |
|---|---|
| `tools/rlm_tool.py` (modify) | Add `durable: bool` (default false) to `RLM_SCHEMA` + the handler. Durable block: require `temporal.enabled` (error, no fallback); resolve the originating `session_key`; assemble the JSON-serializable rlm payload (query/context/input_path/primary_agent/sub_agent/max_global_calls + resolved rlm config — NOT raw secrets); call `dispatch_durable_rlm`; return `{status: "dispatched", run_id}`. |
| `plugins/temporal/tools.py` (modify) | `dispatch_durable_rlm(*, rlm_args, session_key) -> dict` — start `RlmRunWorkflow` with `id=f"durable-rlm-{uuid}"`, return `{status: "dispatched", run_id}` (mirror `dispatch_durable_delegation`). `list_completed_durable_rlm()` — query completed `RlmRunWorkflow`s missing from the outbox, for reconcile (mirror `list_completed_durable_delegations`). |
| `plugins/temporal/workflows.py` (modify) | `RlmRunWorkflow` (module-level in the `try` block) + `_make_rlm_run_workflow()` in both branches. One `execute_activity("run_rlm_durable", payload)` with `retry_policy=RetryPolicy(maximum_attempts=payload["max_attempts"])` and `start_to_close_timeout` from the payload. Returns the full result block + session_key (so reconcile can route it — same lesson as the Phase-2 follow-up). |
| `plugins/temporal/activities.py` (modify) | `run_rlm_durable(payload)` — bootstraps tool discovery, reconstructs the local rlm env, runs the rlm invocation (the same `_run_rlm_in_env` path the sync tool uses), and on completion/failure records a block `{goal, summary, status, result/error, usage, log_path}` to the outbox keyed by `session_key`. Returns `{run_id, session_key, status, block}`. |
| `plugins/temporal/worker.py` (modify) | Register `RlmRunWorkflow` (and `run_rlm_durable` in `_make_activities`). |
| `plugins/temporal/delivery.py` (modify) | Extend `reconcile_from_temporal` to also backfill completed `RlmRunWorkflow`s via `list_completed_durable_rlm` (or a parallel call), into the outbox for the drain. |
| `hermes_cli/config.py` (modify) | Add `rlm.durable_max_attempts` default `2`. |
| `AGENTS.md` (modify) | Document `rlm(durable=true)` under the rlm and/or Temporal sections. |

### Payload shape (JSON-serializable, no raw secrets)

`dispatch_durable_rlm` sends the workflow a payload that the worker can fully
re-execute from:
```
{
  "rlm_args": { query, context?, input_path?, primary_agent?, sub_agent?,
                max_global_calls?, <resolved rlm config keys: executor, kernel_*,
                timeout_seconds, budgets, engine_path, …> },
  "session_key": "<originating session>",
  "run_id": "durable-rlm-<uuid>",
  "max_attempts": <rlm.durable_max_attempts>,
  "timeout_seconds": <rlm.timeout_seconds>
}
```
Credentials are NOT in the payload: the worker resolves rlm credentials from its
own environment/config at activity time, exactly as the sync tool does on the
caller side (`_resolve_rlm_credentials`). This keeps secrets out of the Temporal
history.

### Delivery & reconcile (reused rail)

The activity records the completed result to the durable outbox keyed by
`session_key`. The existing `drain_outbox_for_sessions` delivers it back into the
originating conversation as a completion (the sanctioned re-entry mechanism that
preserves prompt caching), and `durable_status(run_id)` reports running/completed/
failed in the meantime. On startup, `reconcile_from_temporal` backfills any
`RlmRunWorkflow` results that completed while the CLI/gateway was down.

## Crash & error handling

- **Host/worker crash mid-run** → Temporal re-runs `run_rlm_durable` from scratch,
  up to `rlm.durable_max_attempts` (default 2). Kernel state is gone, so this is a
  full re-run (at-least-once full execution); rlm's own `max_money_spent` /
  `max_global_calls` budgets bound each attempt. A single transient crash recovers
  automatically; cost is capped at ~Nx in the worst case.
- **`durable=true` but `temporal.enabled` false** → error from the tool, no
  fallback. The plain `rlm` call still works.
- **rlm unavailable on the worker host** (no Deno / fast-rlm) → activity fails;
  after retries a failure block is recorded to the session via the outbox.
- **Temporal unreachable at dispatch** → error returned to the tool; the user can
  fall back to a plain (non-durable) `rlm` call.
- **At-least-once delivery** is inherited from the Phase-2/3 rail (outbox rows are
  drained idempotently; reconcile skips rows already recorded).

## Limitations (documented)

- **Worker-locality.** `run_rlm_durable` runs rlm with the **local backend on the
  worker host**, so that host must have Deno + fast-rlm installed (and docker/KVM
  if `kernel_sandbox: docker`/microVM runtimes are configured). Durable mode over
  remote Hermes backends (ssh/modal/daytona) is out of scope for this phase.
  Mirrors the Phase-4a/4b worker-locality limitations.
- **Cost on re-run.** A crash re-runs the whole rlm invocation, re-spending
  tokens up to the attempt cap. Stated up front so it isn't a surprise.

## Testing

Per repo conventions (temp `HERMES_HOME`, `scripts/run_tests.sh` wrapper, gated
e2e skips without the `temporal` extra):

- **Unit (no server):**
  - rlm tool durable routing: `durable=true` with `temporal.enabled` false → error
    (no dispatch); with it true → `dispatch_durable_rlm` called with the correct
    payload + resolved `session_key`; non-durable path unchanged.
  - payload assembly carries the rlm args/config and **no raw secrets**.
  - `RlmRunWorkflow` retry `maximum_attempts == rlm.durable_max_attempts`.
  - `run_rlm_durable` records a success block on a stubbed successful run and a
    failure block on a stubbed failure (rlm driver stubbed; assert the outbox row
    + session_key).
- **Gated e2e (time-skipping / dev-server):**
  - `RlmRunWorkflow` → `run_rlm_durable` (stubbed rlm run) → result block lands in
    the outbox → `drain_outbox_for_sessions` delivers it to the session.
  - reconcile backfills a completed `RlmRunWorkflow` the consumer missed.

## Footprint Ladder justification

Adds one parameter to the existing `rlm` tool seam, one workflow + one activity to
the existing temporal plugin, and reuses the Phase-0 worker and the Phase-2/3
outbox/drain/reconcile delivery rail wholesale. No new core tool; opt-in per call
and gated on `temporal.enabled`; fully inert otherwise. The highest-cost option
(step-level checkpoint/resume) is explicitly a non-goal.
