# Design: Temporal Phase 2 — crash-durable background delegation

**Date:** 2026-06-26
**Status:** Approved (design)
**Part of:** "Temporal for better agents" (5-phase effort). This spec is **Phase 2**.
Builds on Phase 0+1 (merged, PR #17): the `plugins/temporal/` plugin, the
`DurableRunWorkflow`, retrying activities, the `delegate_task`-backed step executor,
and the `durable_run`/`durable_status` tools.
**Deferred to their own specs (agreed phasing):** P3 (signals / human-in-the-loop),
P4 (cron/kanban backend swap).

## Context

`delegate_task(background=true)` (today) dispatches a subagent on a **module-level
daemon `ThreadPoolExecutor`** (`tools/async_delegation.py`) and returns a handle
immediately. On completion it pushes a rich, self-contained completion event onto the
**in-memory `process_registry.completion_queue`**; the CLI (`cli.py` process_loop) and
gateway (`_run_process_watcher`) drain that queue while idle and forge a fresh
user/internal turn from each event — never splicing mid-loop, preserving strict
role alternation and the prompt cache.

**Both halves are process-local.** If the process restarts, in-flight background
subagents *and* any undelivered results are lost. AGENTS.md explicitly calls this out:
"background `delegate_task` is detached from the current turn but still process-local.
For work that must survive process restart, use `cronjob` or
`terminal(background=True, notify_on_complete=True)`." Phase 2 closes that gap for
delegation: run the child as a **Temporal workflow** (survives restart) and make
**result delivery survive restart** too — while leaving the existing in-memory async
path untouched when durability isn't requested.

### Invariants (unchanged from Phase 0+1)
1. **Prompt caching is sacred** — results re-enter only via the existing
   completion-queue drain (a fresh idle turn), never spliced into a running loop.
2. **Narrow waist** — additive opt-in behind a flag; the synchronous agent loop and
   the system prompt are untouched; no new core tool.

## Goals

- A new opt-in `delegate_task(background=true, durable=true)` that runs the child as a
  Temporal workflow and **survives a process restart**, with the result delivered back
  to the originating conversation after restart.
- **Durable delivery rail** (the three roles approved as one rail, not parallel engines):
  1. **Durable outbox** — completed durable delegations are recorded in a per-session
     durable store; the existing completion-queue drain delivers undelivered entries
     and marks them delivered (idempotent). This is the auto re-entry.
  2. **On-demand status** — `durable_status(run_id)` (from Phase 1) always works,
     independent of the outbox.
  3. **Startup reconciliation** — on startup, backfill the outbox from Temporal for
     completed durable delegations not yet recorded (covers "finished while no Hermes
     process was alive" / a lost outbox write). Feeds the same outbox+drain rail.
- **Session-key routing that survives restart** (the folded-in nuance) for both CLI
  and gateway sessions.
- **Zero regression**: with `durable=false` (default) or `temporal.enabled: false`,
  behavior is exactly today's in-memory async path.

## Non-goals (Phase 2)

- P3 signals / human-in-the-loop (separate spec).
- P4 cron/kanban backend swap (separate spec).
- Changing the synchronous (non-background) `delegate_task`.
- Cross-process *live* delivery to an already-running peer process (delivery is via the
  durable outbox drained by the owning session; no inter-process push).

## Architecture

```
delegate_task(background=true, durable=true)
  └─ dispatch BackgroundDelegationWorkflow(goal, ctx, session_key, …)   [returns handle now]
        │  (thin wrapper over Phase 1 DurableRunWorkflow: a single step = the goal)
        ▼
   Temporal server (dev-server / Cloud) ── worker (hermes temporal worker)
        │  run_step activity → real delegate_task child (Phase 1 activity + approval-cb fix)
        ▼  on completion
   DURABLE OUTBOX (SQLite under HERMES_HOME)  ◀── startup reconciliation backfills from Temporal
        │  row: (run_id PK, session_key, status, completion_block JSON, created_at, delivered_at NULL)
        ▼  drained by the owning session
   existing completion_queue drain (CLI process_loop / gateway _run_process_watcher)
        │  forge a fresh idle turn from the completion_block; mark row delivered
        ▼
   conversation (prompt-cache-safe)
```

### Components / files

| File | Responsibility |
|---|---|
| `plugins/temporal/workflows.py` (modify) | add `BackgroundDelegationWorkflow` (1-step wrapper over the existing durable-run logic; tagged with session metadata via workflow memo/args). |
| `plugins/temporal/outbox.py` (create) | the durable outbox: `record_completion(run_id, session_key, status, block)`, `claim_undelivered(session_key) -> rows`, `mark_delivered(run_id)`. SQLite under `get_hermes_home()/temporal_outbox.db`; idempotent on `run_id`. |
| `plugins/temporal/activities.py` (modify) | on durable-delegation completion the worker calls `outbox.record_completion(...)` with the rich completion block (reuse the task-source block shape async_delegation already builds). |
| `plugins/temporal/delivery.py` (create) | `drain_outbox_for_sessions(session_keys) -> list[event]` — converts undelivered outbox rows into `type="async_delegation"` completion events (same shape as today) and marks delivered; `reconcile_from_temporal()` — startup backfill. |
| `tools/delegate_tool.py` (modify) | `delegate_task` gains `durable: bool` (default False); when `background and durable`, route to the Temporal dispatch path instead of `dispatch_async_delegation`. Gated on `temporal.enabled`; errors clearly if temporal unreachable (no silent fallback). |
| `cli.py` / gateway watcher (modify, minimal) | call `delivery.drain_outbox_for_sessions(...)` from the existing idle drain + once at startup (`reconcile_from_temporal()` then drain). No new loops — hook into the existing completion-queue drain points. |

### Session-key routing (CLI vs gateway)

The completion block already carries `session_key` (captured at dispatch from
`tools.approval.get_current_session_key`; `<cli>`/empty for the CLI's single session,
a gateway `build_session_key` value per platform conversation). Phase 2 persists it on
the outbox row. Delivery routes by it:

- **CLI:** one logical session. On startup/idle the CLI drains outbox rows whose
  `session_key` matches the local session (and legacy empty/`<cli>` rows), forges
  turns, marks delivered.
- **Gateway:** the process-watcher already iterates live sessions; for each, it drains
  outbox rows matching that `session_key`. A row whose session never returns (platform
  removed, conversation gone) stays undelivered and is surfaced via `durable_status` /
  a `hermes temporal list` listing — never lost, never misdelivered.
- **Idempotency / no double-delivery:** `claim_undelivered` + `mark_delivered` are a
  single atomic SQLite transaction keyed by `run_id`; only one drainer can claim a row.

### Data flow (durable path)

1. `delegate_task(background=true, durable=true)` → resolve session_key → start
   `BackgroundDelegationWorkflow` (id = a stable `durable-deleg-<uuid>`), return the
   handle immediately (same UX as async today).
2. Worker runs the workflow → `run_step` activity → real `delegate_task` child.
3. On completion, worker writes the rich completion block to the outbox (idempotent).
4. Owning session's drain (startup + idle) claims undelivered rows for its session_key,
   emits completion events onto `completion_queue`, marks delivered.
5. Startup reconciliation backfills the outbox from Temporal for anything missed.

## Error handling

- `durable=true` while `temporal.enabled` is false or the server is unreachable → the
  dispatch returns a clear error (mirrors the Phase 1 runtime preflight); **no silent
  fallback** to the non-durable in-memory path.
- Activity failure → Phase 1 RetryPolicy; terminal failure is recorded in the outbox
  with `status="failed"` and delivered as a failure completion (the agent can re-dispatch).
- Outbox write idempotent on `run_id`; `delivered_at` guard prevents re-entry.
- Reconciliation only inserts rows absent from the outbox; never resurrects delivered ones.
- Worker not running → workflow stays queued; `durable_status` reports `pending`.

## Testing

Per repo conventions (temp `HERMES_HOME`, wrapper, no change-detector tests):

- **Unit (no server):** outbox `record/claim/mark` idempotency + atomic claim;
  session-key filtering (CLI empty/`<cli>` vs gateway key; no cross-session leakage);
  `delivery.drain_outbox_for_sessions` produces correctly-shaped `async_delegation`
  events and marks delivered; `delegate_task(durable=true)` gating + **regression test
  that `durable=false`/temporal-disabled is byte-for-byte the existing async path**.
- **Gated e2e (skips without `temporal` binary, like Phase 1):** dispatch a durable
  delegation against `start-dev`; **simulate restart** by draining the outbox from a
  fresh delivery instance (new "process") and assert the result is delivered exactly
  once; assert reconciliation backfills a completed workflow whose outbox row was
  removed.

## Footprint Ladder justification

Additive opt-in on an existing tool (`delegate_task` flag) + plugin-internal modules
(outbox/delivery/workflow), reusing the proven completion-queue rail. No core tool, no
loop changes, no system-prompt changes; fully inert when `durable=false`/temporal off.
