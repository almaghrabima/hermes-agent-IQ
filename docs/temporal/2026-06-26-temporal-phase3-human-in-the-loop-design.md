# Design: Temporal Phase 3 — human-in-the-loop via signals

**Date:** 2026-06-26
**Status:** Approved (design)
**Part of:** "Temporal for better agents" (5-phase effort). This spec is **Phase 3**.
Builds on Phase 0+1 (PR #17: plugin, `DurableRunWorkflow`, retrying activities,
`durable_run`/`durable_status`, worker) and Phase 2 (PR #20: durable outbox + delivery
rail, `reconcile_from_temporal`, `delegate_task(durable=true)`).
**Deferred to its own spec:** P4 (cron/kanban backend swap).

## Context

Hermes already has a **synchronous, per-session** human-approval system
(`tools/approval.py` + gateway `_pending_approvals` + `/approve`/`/deny`): the agent
blocks mid-turn on a dangerous tool call and a human resolves it in the same live
session. That mechanism is turn-scoped and process-local — it cannot represent an agent
that pauses for hours/days awaiting human input and survives a restart.

Phase 3 adds exactly that, using a **Temporal signal**: a durable workflow blocks on
`workflow.wait_condition(...)` (with a timer) until a human sends a signal, then resumes
and delivers its answer back through the **Phase 2 outbox rail**. It is an additive,
opt-in capability (a new `durable_ask` tool) — it does NOT change the existing approval
system.

### Invariants (unchanged)
1. **Prompt caching is sacred** — the answer (and the initial "waiting" notice) re-enter
   only via the existing completion-queue drain (a fresh idle turn), never spliced.
2. **Narrow waist** — additive opt-in plugin tool, `check_fn`-gated; no core tool; the
   synchronous loop and system prompt are untouched.

## Goals

- **`durable_ask(prompt, choices?, timeout_seconds?, context?)`** — start a durable
  `HumanInputWorkflow` that pauses for human input and returns a `run_id` immediately.
- **Response channel** — `hermes temporal respond <run_id> "<answer>"` (CLI) and
  `/respond <run_id> <answer>` (gateway) send a Temporal signal that resumes the
  workflow.
- **Survives restart** — the pending question is recorded durably (a "waiting" outbox
  notice), and the answer re-enters via the Phase 2 delivery rail after resume.
- **Timeout** — configurable `timeout_seconds` (default **86400 = 1 day**); on expiry the
  workflow completes `status="timed_out"` with no answer (delivered like any result).
  No auto-default-response in P3 (YAGNI).
- **Authorization** — the workflow records its originating `session_key`; `respond` is
  **restricted to that session** by default (the `run_id` is the capability).
- **Zero regression** — the existing `tools/approval.py` / `/approve` system is untouched.

## Non-goals (Phase 3)

- P4 cron/kanban backend swap.
- Replacing or rerouting the existing synchronous tool-approval system.
- Auto-default-response on timeout (left out; the agent decides on `timed_out`).
- Multi-party / arbitrary-responder authorization (session-restricted only).

## Architecture

```
durable_ask(prompt, choices, timeout_seconds, context)
  └─ start HumanInputWorkflow(prompt, choices, session_key, run_id, timeout_seconds)  [returns run_id now]
  └─ write "waiting" notice to the Phase-2 outbox  (so the human sees the pending ask; survives restart)
        ▼
   Temporal server ── worker (hermes temporal worker)
        │  workflow: register signal handler `respond(answer)`;
        │  await workflow.wait_condition(lambda: answered, timeout=timedelta(seconds=timeout_seconds))
        ▼
   human: `hermes temporal respond <run_id> "<answer>"`  /  gateway `/respond <run_id> <answer>`
        │  → client.get_workflow_handle(run_id).signal("respond", answer)
        ▼
   workflow resumes → status "answered" (or "timed_out" on timer) → record_outbox activity
        ▼
   Phase-2 delivery rail (outbox → completion_queue drain) → conversation (prompt-cache-safe)
```

### Components

| File | Responsibility |
|---|---|
| `plugins/temporal/workflows.py` (modify) | `HumanInputWorkflow` (module-level inside the `try` block): `@workflow.signal respond(answer)`, `wait_condition` with timer, builds the result block (answered/timed_out) and calls the `record_outbox` activity. Add `_make_human_input_workflow()` mirroring the existing `_make_*` (returns class in try branch; curated ImportError in except). |
| `plugins/temporal/tools.py` (modify) | `dispatch_human_input(*, prompt, choices, context, session_key, timeout_seconds) -> dict` (start workflow, write "waiting" outbox notice, return `{status:"waiting", run_id}`); `signal_human_input(run_id, answer, session_key) -> dict` (authz check + `signal("respond", answer)`); `DURABLE_ASK_SCHEMA` + `handle_durable_ask`. Extend `durable_status` to report `waiting_for_input` + the prompt while pending. |
| `plugins/temporal/outbox.py` (reuse) | no schema change — the "waiting" notice is an ordinary `record_completion` row with `status="waiting"` and a distinct run_id (`<run_id>:waiting`) so it is separate from the eventual answer row (`<run_id>`). Each is delivered once via the normal `delivered_at` mechanism; no "superseded" logic is needed (a delivered waiting notice never re-surfaces). |
| `plugins/temporal/__init__.py` (modify) | register `durable_ask` (toolset `temporal`, `check_fn=temporal_available`). |
| `plugins/temporal/worker.py` (modify) | register `HumanInputWorkflow`. |
| `hermes_cli` + `gateway` (modify) | `hermes temporal respond <run_id> "<answer>"` subcommand (extend the existing `temporal` CLI command) + a `/respond <run_id> <answer>` gateway slash command, both calling `signal_human_input` with the resolved `session_key`. |

### Authorization

`HumanInputWorkflow` is started with `session_key` (from
`tools.approval.get_current_session_key(default="default")`) in its args. `respond`
resolves the caller's session_key the same way; `signal_human_input` fetches the
workflow's recorded session_key from the outbox `<run_id>:waiting` row (written at
dispatch, locally available without the worker) and **rejects a mismatch** with a clear
error. CLI is single-session
(trivially matches); gateway `/respond` is honored only from the originating
conversation. The `run_id` itself is required and acts as a capability.

### Data flow (happy path)

1. Agent calls `durable_ask("Approve deploy to prod? (yes/no)", choices=["yes","no"], timeout_seconds=3600)`.
2. Tool starts `HumanInputWorkflow`, writes a `waiting` outbox notice for the session,
   returns `{status:"waiting", run_id}` inline; the agent tells the user how to respond.
3. Human runs `hermes temporal respond <run_id> "yes"`.
4. Worker's workflow `wait_condition` unblocks → result `{status:"answered", answer:"yes"}`
   → `record_outbox` → Phase-2 drain delivers it as a fresh turn.
5. (timeout variant) timer fires first → `{status:"timed_out", answer:None}` delivered.

## Error handling

- `durable_ask` gated on `temporal.enabled` (no silent fallback, mirrors P2).
- `respond` to an unknown/closed `run_id` → clear error (`signal` raises; surfaced).
- `respond` from a non-matching session → authz error.
- Duplicate `respond` after answered → ignored (the signal handler no-ops once answered;
  idempotent).
- timer/signal race resolved by `wait_condition` (whichever sets the condition / fires
  first wins; the result reflects it).
- `record_outbox` retried (Phase 2 RetryPolicy); the "waiting" notice and the answer are
  distinct outbox rows so delivery of the answer is never blocked by the notice.

## Testing

Per repo conventions (temp `HERMES_HOME`, wrapper, no change-detector tests):

- **Unit (no server):** `durable_ask` gating + arg validation (prompt required; choices
  optional); `signal_human_input` authz (session mismatch rejected) with a mocked client;
  `durable_status` waiting-state shape; the "waiting" outbox notice uses a distinct row id
  from the answer.
- **Gated e2e (skips without `temporal`; time-skipping env):**
  - **signal path:** start `HumanInputWorkflow`, send the `respond` signal, assert it
    resumes and the outbox delivers `status="answered"` with the answer exactly once.
  - **timeout path:** start with a short `timeout_seconds`, advance skipped time, assert
    it completes `status="timed_out"` and is delivered.

## Footprint Ladder justification

Additive opt-in tool + plugin-internal workflow/CLI, reusing Phase 1 (workflow/worker)
and Phase 2 (outbox delivery). No core tool, no loop/system-prompt changes, the existing
approval system untouched; fully inert when `temporal.enabled` is false.
