# Design: Temporal Phase 4b — durable kanban spawn backend

**Date:** 2026-06-26
**Status:** Approved (design)
**Part of:** "Temporal for better agents" (5-phase effort). This is **Phase 4b**
(kanban) — the final piece. P4 was decomposed into two independent subsystems:
**P4a (cron, PR #27, merged)** and **P4b (kanban, this spec)**.
Builds on Phase 0+1 (PR #17: plugin, worker, config, gating) and reuses the same
`hermes temporal worker` that Phases 2/3/4a host.

## Context

Kanban is **both** a dashboard and an active execution system. A card flows
through a SQLite state machine (`triage → todo → ready → running → done/blocked`)
and, when `ready`, the **dispatcher** claims it and spawns a worker to do the work.

The dispatcher tick (`hermes_cli/kanban_db.py` `dispatch_once` →
`_dispatch_once_locked`, line 6556) does two separable things:

1. **Orchestration** (all SQLite): reclaim stale runs, promote `ready` candidates
   when dependencies clear (`recompute_ready`), claim with a TTL lease
   (`claim_task`), enforce circuit-breaker (`failure_limit`) and concurrency caps.
2. **Execution**: `_default_spawn(task, workspace, board)` (line 7286)
   `subprocess.Popen`s `hermes -p <profile> … chat -q "work kanban task <id>"` with
   `HERMES_KANBAN_TASK=<id>` (and workspace/board/run-id) in the env, then returns
   the PID. The subprocess reads its context from the env, does the work, and calls
   the `kanban_complete` / `kanban_block` tools to transition the card. The
   dispatcher tracks `task_runs.worker_pid` and reclaims the task if the PID dies
   (`detect_crashed_workers`, `release_stale_claims`).

**Crucially, `dispatch_once`/`_dispatch_once_locked` already take a `spawn_fn`
parameter** (line 6559/6625), defaulting to `_default_spawn` when `None`. Tests
already inject stubs through it. So a durable backend needs **no new abstraction**
— the injection seam exists. (This is lighter than cron P4a, which had to add a
`CronScheduler` ABC.)

The current crash-durability is **best-effort and requires the dispatcher to be
alive**: if the host/gateway process is down, nothing notices a dead worker and
nothing re-runs the card. P4b makes a card's worker **crash-durable** — Temporal
holds the run and re-executes it on worker return, independent of the dispatcher
process.

### Invariants
1. **Narrow waist / opt-in** — a `spawn_fn` selected only by
   `kanban.spawn_provider: temporal`; built-in `_default_spawn` stays the default;
   zero regression when unselected.
2. **Never leave kanban without a spawn** — the resolver falls back to the
   built-in spawn if temporal isn't enabled/available, and an individual spawn
   that can't reach Temporal falls back to `_default_spawn` for that tick. No card
   is ever dropped.
3. **At-most-once per card run** — exactly one supervisor is authoritative for a
   Temporal-backed run, so a card never executes twice (see "The
   double-execution resolution").

## Goals

- A temporal `spawn_fn` under `plugins/kanban_spawn_temporal/`, selected via
  `kanban.spawn_provider: temporal`, that starts a **`KanbanTaskWorkflow(task_id)`**
  on the Phase-0 worker instead of a local `subprocess.Popen`.
- The workflow runs a single activity, `run_kanban_worker`, which `Popen`s **the
  same `hermes chat` subprocess** (same env/profile/workspace as `_default_spawn`)
  and heartbeats while it runs — Temporal supplies durable retry + supervision; the
  subprocess's `kanban_complete`/`kanban_block` calls transition the card exactly
  as today.
- Temporal is the **sole supervisor** for tasks it spawns; SQLite crash-detection
  / reclaim skips Temporal-backed runs to prevent double-execution.
- Built-in subprocess spawn remains the default; fallback on unavailability.

## Non-goals (Phase 4b)

- **Event-driven dispatch.** The 60s polling tick (`_kanban_dispatcher_watcher`,
  `gateway/kanban_watchers.py:643`) stays; P4b changes only the spawn, not the
  tick. (Considered and rejected for scope — it rewrites the gateway watcher.)
- **Full orchestration in Temporal.** Claim/promote/dependency/circuit-breaker/caps
  stay in SQLite; they are battle-tested and out of scope to re-implement.
- **In-process agent execution.** The activity Popens a subprocess; it does **not**
  run the agent in-process via the Phase-2 `execute_durable_step` path. Kanban's
  per-card process/profile/branch isolation is preserved verbatim.
- **Cross-host workspaces** (see Limitations) — a flagged follow-up.

## Architecture

```
kanban dispatch tick  (UNCHANGED: claim / promote / dependency / circuit-breaker — all SQLite)
   └─ spawn step ── resolve_kanban_spawn(cfg) ──▶ [builtin]  _default_spawn → subprocess.Popen   (default)
                                              └─▶ [temporal] start KanbanTaskWorkflow(task)       (opt-in)
                                                       │
                            hermes temporal worker ────┘
                              KanbanTaskWorkflow(task_id, spawn_args)
                                └ activity: run_kanban_worker(spawn_args)
                                     ├ Popen the SAME `hermes chat` subprocess (same env/profile/workspace)
                                     ├ activity.heartbeat() while alive   ← Temporal supervision
                                     └ subprocess calls kanban_complete/block → SQLite transition (UNCHANGED)
```

### Components

| File | Responsibility |
|---|---|
| `hermes_cli/kanban_db.py` (modify) | In `_dispatch_once_locked`, when `spawn_fn is None`, call `resolve_kanban_spawn(cfg)` instead of hardcoding `_default_spawn`. Extract the env+argv construction out of `_default_spawn` into a pure `build_spawn_args(task, workspace, board) -> dict` so both backends share one source of truth. `_default_spawn` becomes `build_spawn_args` + `Popen`. Add a `run_kind` marker (or `worker_pid` sentinel) on the run row so reclaim can identify Temporal-backed runs. |
| `hermes_cli/kanban_spawn_provider.py` (create) | `resolve_kanban_spawn(cfg) -> spawn_fn`. Reads `kanban.spawn_provider` (default `"builtin"` → `_default_spawn`). `"temporal"` → the temporal spawn, **falling back to `_default_spawn`** if `temporal.enabled` is false / the plugin can't load. Config-only availability check (no network), mirroring cron's `resolve_cron_scheduler` contract. |
| `plugins/kanban_spawn_temporal/__init__.py` (create) | The temporal `spawn_fn(task, workspace, board)`: `args = build_spawn_args(...)`; `client.start_workflow("KanbanTaskWorkflow", args, id=f"hermes-kanban-{task.id}-{run_id}", task_queue=…)`; returns a non-None launch sentinel so the dispatcher records the run as launched (parity with `_default_spawn` returning a PID). On any connect/start error: log and **return `_default_spawn(task, workspace, board)`** (per-tick fallback). |
| `plugins/temporal/workflows.py` (modify) | `KanbanTaskWorkflow` (module-level in the `try` block + `_make_kanban_task_workflow()` in both branches, matching the existing pattern). One `execute_activity("run_kanban_worker", args)` call with `RetryPolicy(maximum_attempts = 1 + kanban.failure_limit)` and `start_to_close_timeout` from the task's `max_runtime_seconds`. |
| `plugins/temporal/activities.py` (modify) | `run_kanban_worker(spawn_args) -> dict`: `Popen` the subprocess from `spawn_args`; loop polling `proc.poll()` and calling `activity.heartbeat()` on an interval; on heartbeat also refresh the SQLite claim lease so the TTL never expires under a live run. Return `{exit_code,…}`. Bootstraps tool discovery as the worker already does (`discover_builtin_tools()`). |
| `plugins/temporal/worker.py` (modify) | Register `KanbanTaskWorkflow` + `run_kanban_worker`. |
| `hermes_cli/config.py` (modify) | Add `kanban.spawn_provider` default `"builtin"` to the kanban config block. |

### `build_spawn_args` (the shared core)

Extracted verbatim from today's `_default_spawn` so both backends are byte-identical
in what they run:

```
build_spawn_args(task, workspace, board) -> {
    "argv": [hermes, "-p", profile, "--accept-hooks", "--skills", …, "chat",
             "-q", f"work kanban task {task.id}"],
    "cwd": workspace,
    "env": { HERMES_KANBAN_TASK, HERMES_KANBAN_WORKSPACE, HERMES_KANBAN_BOARD,
             HERMES_KANBAN_RUN_ID, HERMES_KANBAN_GOAL_MODE?, … },
    "max_runtime_seconds": task.max_runtime_seconds,
}
```

`_default_spawn` = `build_spawn_args` → `subprocess.Popen` (unchanged behavior).
`run_kanban_worker` = `build_spawn_args`-output (passed through the workflow) →
`subprocess.Popen` + heartbeat. Identical argv/env on both paths is a tested
invariant.

### The double-execution resolution (the crux)

Temporal becomes the **sole supervisor** for tasks it spawns:

- The activity heartbeats; on worker/host crash Temporal reschedules the activity
  and re-runs the subprocess — the durability win that SQLite's PID-poll cannot
  provide (PID-poll needs the dispatcher process alive).
- To stop SQLite from *also* reclaiming and re-spawning the same card, the temporal
  spawn marks the run as Temporal-backed (a `run_kind="temporal"` column, or a
  `worker_pid` sentinel), and `detect_crashed_workers` / `release_stale_claims`
  **skip Temporal-backed runs**. SQLite is no longer a competing supervisor for
  those runs.
- The activity periodically refreshes the SQLite claim heartbeat
  (`last_heartbeat_at` / `claim_expires`) so the TTL lease never expires under a
  live Temporal run — preventing a stale-claim reclaim from racing the activity.
- `RetryPolicy.maximum_attempts = 1 + kanban.failure_limit` aligns Temporal's
  retry budget with the existing circuit-breaker budget, so the two don't fight.
  On terminal failure, the subprocess's own `kanban_block` (or, if the subprocess
  never started, a final blocking step) lands the card in `blocked`, exactly as
  today.

## Error handling

- `kanban.spawn_provider: temporal` but `temporal.enabled` false / plugin import
  fails → `resolve_kanban_spawn` logs and returns `_default_spawn`. Kanban runs
  normally (non-durable).
- Temporal unreachable at spawn time → `start_workflow` raises → the temporal
  `spawn_fn` catches, logs, and **returns `_default_spawn(task, workspace, board)`
  for that tick** (the card still runs, just non-durably); the next tick retries
  temporal. No card is dropped.
- Worker down when a card is claimed → the workflow is already started and durable;
  Temporal runs `run_kanban_worker` when the worker returns. SQLite reclaim does
  not fire (Temporal-backed run is skipped).
- Subprocess exits non-zero / times out → activity returns failure / raises;
  Temporal retries up to `maximum_attempts`; terminal failure routes the card to
  `blocked` via the existing failure path.
- Duplicate concern (a stale tick + the activity) → SQLite `claim_task` CAS still
  gates the *initial* claim at-most-once; after claim, Temporal is the only
  supervisor, so no second run is launched.

## Limitations (documented)

- **Workspace locality.** `run_kanban_worker` `Popen`s on the **worker host**, so
  the worker must be able to reach the card's `workspace_path` (same host, or a
  shared filesystem). Cross-host workspaces (e.g. worker on a different machine
  than the board) are out of scope for P4b and a flagged follow-up. Stated up front
  so it isn't a surprise.
- **Dispatch latency unchanged.** P4b makes the *run* durable, not the *trigger*;
  the 60s polling tick still gates how quickly a `ready` card is picked up.

## Testing

Per repo conventions (temp `HERMES_HOME`, `scripts/run_tests.sh` wrapper, gated
e2e skips without the `temporal` extra):

- **Unit (no server):**
  - `build_spawn_args` parity — the temporal path and `_default_spawn` produce
    identical `argv`/`env`/`cwd` for the same task (one source of truth).
  - `resolve_kanban_spawn` selection + fallback — default → `_default_spawn`;
    `temporal` with `temporal.enabled=false` → `_default_spawn`; `temporal` with
    import failure → `_default_spawn`.
  - Per-tick fallback — temporal `spawn_fn` with a connect error invokes
    `_default_spawn` and the card is still launched.
  - Reclaim skip — a run marked Temporal-backed is skipped by
    `detect_crashed_workers` / `release_stale_claims` (and a builtin run is NOT
    skipped — guard against over-skipping).
  - Retry budget — `KanbanTaskWorkflow` retry `maximum_attempts == 1 +
    kanban.failure_limit`.
- **Gated e2e (time-skipping / dev-server):**
  - Claimed task → `KanbanTaskWorkflow` → `run_kanban_worker` (stubbed `Popen`)
    heartbeats and returns success; card reaches `done` via the stubbed
    `kanban_complete`.
  - SQLite reclaim does **not** fire for a Temporal-backed run while the activity
    is live.
  - Terminal activity failure (subprocess non-zero past `maximum_attempts`) routes
    the card to `blocked`.

## Footprint Ladder justification

Uses kanban's **existing** `spawn_fn` injection seam (no new ABC, no new core
tool), reuses the Phase-0 `hermes temporal worker` and the **verbatim** subprocess
contract (same argv/env/isolation), and is selected by a single config key
(`kanban.spawn_provider: temporal`). Built-in subprocess spawn stays the default;
the system is fully inert unless explicitly opted in. The highest-blast-radius
options (event-driven tick, full Temporal orchestration) are explicitly deferred as
non-goals.
