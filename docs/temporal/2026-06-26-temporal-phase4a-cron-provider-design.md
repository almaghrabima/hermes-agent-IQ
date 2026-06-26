# Design: Temporal Phase 4a — durable cron trigger provider

**Date:** 2026-06-26
**Status:** Approved (design)
**Part of:** "Temporal for better agents" (5-phase effort). This spec is **Phase 4a**
(cron). **Phase 4b (kanban backend) is a separate spec** — P4 was decomposed into two
independent subsystems; cron first.
Builds on Phase 0+1 (PR #17: plugin, worker, config, gating) and reuses the Phase-2/3
`hermes temporal worker`.

## Context

Cron already has a **pluggable trigger abstraction** built for exactly this:
`cron/scheduler_provider.py` defines `CronScheduler` (ABC, "Axis-B trigger — decides
*when* a due job fires"), selected via the `cron.provider` config key; its docstring
explicitly anticipates "an external provider (**Chronos, Phase 4**) … under
`plugins/cron_providers/<name>/`." `resolve_cron_scheduler()` loads the named provider
and **falls back to the built-in** if it's missing / fails to load / reports
`is_available() == False` — "cron must never be left without a trigger."

Crucially, **firing is already shared**: `CronScheduler.fire_due(job_id)` claims the job
with a store-level compare-and-set (multi-machine at-most-once) and runs it via
`cron.scheduler.run_one_job`. Providers must NOT reimplement agent construction or
delivery. So P4a only has to (a) decide *when* to fire (Temporal Schedules) and (b)
route the fire back through `fire_due`.

The built-in `InProcessCronScheduler` runs a 60s in-process daemon ticker — so a
scheduled job is **missed entirely if the host process is down**. P4a makes the
*schedule* durable: Temporal holds it and fires it (with catch-up) even across a Hermes
restart, executing the job in the always-on `hermes temporal worker`.

### Invariants
1. **Narrow waist / opt-in** — a provider behind the existing `CronScheduler` interface;
   built-in stays the default; selected only by `cron.provider: temporal`. Zero
   regression when unselected.
2. **Never break cron** — `is_available()` is config-only (no network); if Temporal
   isn't enabled/reachable the resolver falls back to the built-in ticker.

## Goals

- A `TemporalCronScheduler(CronScheduler)` provider under
  `plugins/cron_providers/temporal/`, `name = "temporal"`, selected via
  `cron.provider: temporal`.
- Map each Hermes cron job to a **Temporal Schedule** and keep them reconciled
  (`start`/`on_jobs_changed`/`reconcile`).
- Fire due jobs **durably in the worker** via the existing `fire_due(job_id)` shared
  path — surviving a CLI/gateway restart.
- Reuse the Phase-0 worker (`hermes temporal worker`) to host the fire workflow/activity.
- Built-in remains default; fallback on unavailability.

## Non-goals (Phase 4a)

- P4b kanban backend (separate spec).
- Replacing the built-in scheduler (it stays the default).
- Full gateway-platform delivery from the worker (see Limitations) — boundable follow-up.
- Re-implementing job execution/delivery (stays in `cron.scheduler.run_one_job`).

## Architecture

```
cron job store (jobs.json)                Temporal server (dev / Cloud)
  add/update/remove/pause ──on_jobs_changed/reconcile──▶ Temporal Schedule per job
                                                          id = hermes-cron-<job_id>
                                                          spec: cron|interval|one-shot
                                                          overlap: SKIP
                                                              │ (fires when due; catch-up if worker was down)
                                                              ▼
                                        hermes temporal worker ── CronFireWorkflow(job_id)
                                                              │      └ activity: fire_cron_job_activity(job_id)
                                                              ▼            └ CronScheduler.fire_due(job_id)
                                                       claim_job_for_fire (at-most-once) → run_one_job → deliver
```

### Components

| File | Responsibility |
|---|---|
| `plugins/cron_providers/__init__.py` (verify/extend) | `load_cron_scheduler(name)` already referenced by the resolver — ensure it discovers the `temporal` provider. |
| `plugins/cron_providers/temporal/__init__.py` (create) | `TemporalCronScheduler(CronScheduler)`: `name`, `is_available`, `start`, `stop`, `on_jobs_changed`, `reconcile`. |
| `plugins/cron_providers/temporal/schedules.py` (create) | Pure mapping `job_to_schedule_spec(job) -> ScheduleSpec-args` for the three job types + `schedule_id(job_id)`; and the reconcile diff `plan_reconcile(jobs, existing_ids) -> (create, update, delete)`. |
| `plugins/cron_providers/temporal/client_ops.py` (create) | Thin async wrappers: `create_or_update_schedule`, `delete_schedule`, `list_hermes_schedule_ids` (lazy-import temporalio; reuse `plugins.temporal.client.connect`). |
| `plugins/temporal/workflows.py` (modify) | `CronFireWorkflow` (module-level in the `try` block) + `_make_cron_fire_workflow()`. |
| `plugins/temporal/activities.py` (modify) | `fire_cron_job_activity(job_id)` → `from cron.scheduler_provider import CronScheduler; CronScheduler.fire_due(...)` (or the shared `run_one_job` via the provider default). Bootstraps tool discovery (as the worker already does). |
| `plugins/temporal/worker.py` (modify) | Register `CronFireWorkflow` + `fire_cron_job_activity`. |

### Schedule mapping (the meat)

`cron/jobs.py` `parse_schedule` produces three job kinds; map each to a Temporal
`ScheduleSpec`:
- **cron expression** (`expr`) → `ScheduleSpec(cron_expressions=[expr], time_zone_name=<job tz>)`.
- **interval** (every N seconds) → `ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=N))])`.
- **one-shot** (`run_at`) → `ScheduleSpec(calendars=[<calendar at run_at>])` with
  `ScheduleState(limited_actions=True, remaining_actions=1)` so it fires exactly once,
  then the Schedule auto-completes. (Reconcile deletes it after it's consumed / the job
  is removed.)

All schedules use `ScheduleOverlapPolicy.SKIP` (don't start a new fire if the prior is
still running) to match cron's at-most-once-per-due semantics, and a bounded
**catchup window** mapped from the job's grace seconds (`_compute_grace_seconds`) so a
worker that was briefly down still fires a just-missed job but doesn't replay ancient ones.

The Schedule's **action** = start `CronFireWorkflow(job_id)` on the `temporal.task_queue`.

### Reconciliation

`start()` (called once when cron starts): connect, `list_hermes_schedule_ids()`, diff
against `get_due_jobs`/all jobs via `plan_reconcile`, create/update/delete schedules,
then **return** (the worker drives firing; the provider process holds nothing) while
honoring `stop_event` for teardown. `on_jobs_changed()` and `reconcile()` run the same
diff for the incremental case. Schedule ids are namespaced `hermes-cron-<job_id>` so we
only ever touch our own schedules.

### Firing

`CronFireWorkflow(job_id)` → `fire_cron_job_activity(job_id)` (in the worker) →
`CronScheduler.fire_due(job_id)` — the existing default: `claim_job_for_fire` (store CAS,
at-most-once across machines) → `run_one_job`. No new execution/delivery code.

## Error handling

- `cron.provider: temporal` but `temporal.enabled` false → `is_available()` False →
  resolver falls back to the built-in ticker (logged). Cron keeps working.
- Temporal unreachable at `start()` reconcile → `is_available()` is config-only and
  can't network-probe, so the provider is already selected. Key mitigation: **once
  created, Temporal Schedules fire independently of the provider/main process** (the
  worker drives them), so existing jobs keep firing; only *reconciliation of new/changed
  jobs* is paused. `start()` therefore retries the connect/reconcile on an interval
  (honoring `stop_event`) and logs loudly; when Temporal returns, it converges. Selecting
  `cron.provider: temporal` is an explicit dependency on Temporal being reachable for
  schedule *changes* to propagate — documented, not silent.
- Schedule create/update failure → logged; next `on_jobs_changed`/`reconcile` retries
  (idempotent create-or-update by schedule id).
- Worker down when a schedule fires → Temporal queues the action; fires on worker return
  within the catchup window; beyond it, skipped (bounded, matches grace).
- Duplicate fire (catch-up + a live machine) → `claim_job_for_fire` CAS ensures
  at-most-once.

## Limitations (documented)

- **Gateway-platform delivery from the worker.** `run_one_job` delivers via the cron
  delivery path; gateway chat-platform delivery relies on live gateway adapters that the
  standalone worker does not hold. P4a covers what the worker can reach (the job's
  configured delivery + non-gateway paths); full gateway delivery from the worker is a
  flagged follow-up (e.g. the worker hands a gateway-targeted result to the gateway via
  the same completion-queue rail Phase 2/3 use). Stated up front so it isn't a surprise.

## Testing

Per repo conventions (temp `HERMES_HOME`, wrapper, gated e2e skips without `temporal`):

- **Unit (no server):** `job_to_schedule_spec` for all three kinds (cron/interval/one-shot
  incl. `remaining_actions=1`, overlap SKIP, tz, catchup from grace); `plan_reconcile`
  diff (create/update/delete sets given jobs vs existing schedule ids); `schedule_id`
  namespacing; `is_available()` gating (temporal disabled → False → built-in); provider
  loads via `load_cron_scheduler("temporal")`; `resolve_cron_scheduler()` falls back to
  built-in when unavailable.
- **Gated e2e (time-skipping / dev-server):** register a cron job → schedule created →
  on fire, `fire_cron_job_activity` invokes `fire_due` (with a stubbed `run_one_job`) and
  the job is claimed+run exactly once; one-shot fires once then the schedule is gone;
  `on_jobs_changed` after a job delete removes the schedule.

## Footprint Ladder justification

Implements an existing, purpose-built extension point (`CronScheduler` provider) as an
opt-in plugin under `plugins/cron_providers/temporal/`, reusing the shared `fire_due`
execution path and the Phase-0 worker. No core tool, built-in stays default, fully inert
unless `cron.provider: temporal`. Highest-blast-radius work (kanban) is deferred to P4b.
