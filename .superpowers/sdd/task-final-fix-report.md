# Final Fix Report — Temporal Plugin

Date: 2026-06-25

## FIX 1 (CRITICAL): Worker must register builtin tools before serving

**Problem:** `plugins/temporal/activities.py::_delegate_handler()` accessed
`registry._tools["delegate_task"]` with a raw dict lookup, but the worker
subprocess never triggered builtin tool discovery → `delegate_task` was never
in `registry._tools` → every workflow step raised `KeyError`.

**Discovery entrypoint used:**
```python
from tools.registry import discover_builtin_tools
discover_builtin_tools()
```
Called *inside* `run_worker()` in `plugins/temporal/worker.py`, before
constructing the `Worker` instance. This is idempotent (re-imports are no-ops
after the first call; the registry accumulates registrations).

`model_tools.py` also exports `discover_builtin_tools` (imported from
`tools.registry` and called at module level on line 184), but calling it
directly from `tools.registry` avoids pulling in all of model_tools' other
side effects in the worker subprocess.

**activities.py hardening:** Replaced raw dict subscript with `.get()` +
explicit `RuntimeError` so a missing registration emits a clear diagnostic
instead of a raw `KeyError`.

**Files changed:**
- `plugins/temporal/worker.py` — added `discover_builtin_tools()` call
- `plugins/temporal/activities.py` — `_delegate_handler()` now uses `.get()` + `RuntimeError`

## FIX 1-TEST: Registry seam test

**File added:** `tests/plugins/temporal/test_activities_registry.py`

Two tests:
1. `test_discover_registers_delegate_task` — calls `discover_builtin_tools()`
   and asserts `"delegate_task" in registry._tools`.
2. `test_execute_durable_step_via_real_registry` — confirms discovery works,
   then monkeypatches only the handler (not `_delegate_handler` itself) to
   avoid a real LLM call, and asserts `execute_durable_step` returns
   `{"ok": True, "result": "ok"}`.

Note: `requests` must be installed for `tools.delegate_tool` to import.
`requests==2.33.0` is declared in `pyproject.toml[dependencies]` but was
absent from the test `.venv` — installed via `uv pip install "requests==2.33.0"`
before tests ran.

## FIX 2 (IMPORTANT): Remove dead mTLS config

**Problem:** `TemporalSettings.tls_cert`/`tls_key` and the corresponding
`TEMPORAL_TLS_CERT`/`TEMPORAL_TLS_KEY` env reads existed in `tconfig.py` but
`client.build_connect_kwargs` never consumed them → silently non-functional.

**Action:** Removed the two dataclass fields and the two `env.get(...)` lines
from `resolve_temporal_config`. Temporal Cloud auth via `TEMPORAL_API_KEY`
(which is wired up and functional) is the supported path for Phase 1.

Grep confirmed no other references to `tls_cert`/`tls_key`/`TEMPORAL_TLS_*`
in `plugins/` or `tests/` (only the lines removed).

`tests/plugins/temporal/test_tconfig.py` tested `api_key`, not cert/key — all
3 tests still pass unchanged.

**File changed:** `plugins/temporal/tconfig.py`

## FIX 3 (MINOR docs): AGENTS.md accuracy

**File changed:** `AGENTS.md` (lines ~1175–1193)

- `durable_run` description: replaced "returns a `run_id`; blocks up to
  `wait_seconds` (default 30) then polls" with accurate phrasing: "blocks up
  to `wait_seconds` (default 30) for an inline result, otherwise returns a
  `run_id` to poll with `durable_status`."
- Secrets line: removed `TEMPORAL_TLS_CERT` / `TEMPORAL_TLS_KEY` reference;
  added "(mTLS cert/key: future work.)" note.

## VERIFY outputs

### Unit tests (`scripts/run_tests.sh tests/plugins/temporal/`)
```
Summary: 9 files, 14 tests passed, 0 failed (100% complete) in 0.9s (24 workers)
```
(13 pre-existing + 2 new = 14 unit tests; integration test collected separately)

### Integration e2e (`scripts/run_tests.sh tests/plugins/temporal/test_integration.py -- -m integration -o "addopts="`)
```
Summary: 1 files, 1 tests passed, 0 failed (100% complete) in 0.8s (24 workers)
```

### ruff (`ruff check plugins/temporal/`)
```
All checks passed!
```

---

## Final-review fix wave — Temporal Phase 2 (2026-06-26)

### FIX 1: Faithful reconcile data from `BackgroundDelegationWorkflow`

**File:** `plugins/temporal/workflows.py`

Changed the final `return` in `BackgroundDelegationWorkflow.run` from `{"run_id": ..., "status": ...}` to include the full reconcile payload:
```python
return {"run_id": params["run_id"], "session_key": params.get("session_key", "default"), "status": block["status"], "block": block}
```

**File:** `plugins/temporal/tools.py`

Updated `list_completed_durable_delegations()` to use the real workflow result:
- `session_key` now comes from `res.get("session_key", "default")` (was hardcoded `"default"`)
- `block` now comes from `res.get("block", {})` (was a minimal `{"summary": res.get("status"), "goal": ""}` stub)

### FIX 2: Reject durable batch dispatches

**File:** `tools/delegate_tool.py`

When `durable=true` and `tasks` is non-empty, returns:
```json
{"status":"error","error":"delegate_task durable=true does not support batch (tasks=[...]) in Phase 2; dispatch durable single-goal delegations individually."}
```
before any dispatch happens.

### FIX 3: No silent non-durable fallback when `background` is falsy

**File:** `tools/delegate_tool.py`

Restructured the durable guard so the `if durable:` block now explicitly:
1. Rejects batch (`tasks`) before checking background
2. Returns an explicit error when `background` is falsy (`durable=true requires background=true.`) instead of falling through to the in-process path

Non-durable paths are byte-for-byte unchanged.

### Test additions

**`tests/tools/test_delegate_durable.py`** — added:
- `test_durable_without_background_errors`: durable=True, background=False → status error mentioning "background"
- `test_durable_rejects_batch`: durable=True, background=True, tasks=[{"goal":"a"}] → status error mentioning "batch"
- Existing routing-success test already passes background=True

**`tests/plugins/temporal/test_phase2_integration.py`** — extended:
- Captured `wf_result = await env.client.execute_workflow(...)` return value
- Asserts `wf_result["session_key"] == "sessA"` (FIX 1 session_key fidelity)
- Asserts `"block" in wf_result` and `block["summary"] == "answer"` (FIX 1 real block)
- Existing outbox-drain assertions unchanged

### Verify outputs

```
py_compile: OK (tools/delegate_tool.py plugins/temporal/workflows.py plugins/temporal/tools.py)
ruff check: All checks passed!
Unit tests (tests/tools/test_delegate_durable.py + tests/plugins/temporal/): 26/26 passed, 0 failed (1.2s)
Integration test (test_phase2_integration.py -m integration): 1/1 passed, 0 failed (0.8s)
```

---

## Final-review fix wave — Temporal Phase 3 (2026-06-26)

### BUG FIX: `hermes temporal respond` always failed authz

**Root cause:** `signal_human_input` compared `row.session_key` (set to the live
session key, e.g. `"cli-abc"`) against `get_current_session_key()` from a fresh
subprocess, which always returns `"default"`. Session keys never matched →
every standalone CLI invocation returned "not authorized".

**File:** `plugins/temporal/tools.py`

Added `trusted: bool = False` keyword-only parameter to `signal_human_input`.
When `trusted=True` the session-key check is skipped entirely:
```python
def signal_human_input(run_id, answer, session_key, *, trusted=False):
    ...
    if not trusted and (row.get("session_key") or "default") != (session_key or "default"):
        return {"status": "error", "error": "not authorized: ..."}
```

**File:** `plugins/temporal/worker.py`

`cmd_temporal` respond branch now calls with `trusted=True` (session_key
irrelevant for the local operator). Dead `get_current_session_key` import in
that branch removed:
```python
res = signal_human_input(args.run_id, args.answer, "", trusted=True)
```

**Gateway path untouched:** `plugins/temporal/__init__.py` `_respond_command`
still calls `signal_human_input(run_id, answer, get_current_session_key(...))` —
`trusted` defaults to `False`, session restriction enforced.

### LATENT-CRASH FIX: `HumanInputWorkflow.__init__` missing `_session_key`

**File:** `plugins/temporal/workflows.py`

Added `self._session_key = "default"` in `__init__` alongside `_answer` and
`_answered`. Without it the `get_session_key` query handler raised `AttributeError`
if queried before `run()` bound the attribute.

### New tests

**`tests/plugins/temporal/test_human_input_authz.py`** — added:
- `test_signal_authorized_when_session_matches`: row session "sessA", caller
  "sessA" → `status == "ok"`. (fake async client, no Temporal server needed)
- `test_signal_trusted_bypasses_session_check`: row session "sessA", caller
  "default", `trusted=True` → `status == "ok"`. Proves the fix; without it
  this was always rejected.

**`tests/plugins/temporal/test_respond_command.py`** — added:
- `test_cmd_temporal_respond_uses_trusted`: monkeypatches
  `plugins.temporal.tools.signal_human_input`, builds argparse-like args,
  calls `worker.cmd_temporal(args)`, asserts `trusted=True` reached the call
  site and return code is 0.

### Verify outputs

```
ruff check plugins/temporal/: All checks passed!
Unit tests (tests/plugins/temporal/): 35/35 passed, 0 failed (1.7s)
Integration test (test_phase3_integration.py -m integration): 2/2 passed, 0 failed (1.0s)
```

**Commit:** `d9f6f4a26` — `fix(temporal): trusted local respond bypass + init workflow session_key (Phase 3 final review)`

---

## Final-review fix wave — Temporal Phase 4a (2026-06-26)

### BUG: Silent wrong-time cron firing on non-UTC deployments

**Root cause:** `plugins/cron_providers/temporal/schedules.py` `job_to_spec` used
`job.get("timezone") or "UTC"` to set the `time_zone` field. Cron jobs have NO
per-job `timezone` key — Hermes timezone is a GLOBAL config setting. So
`job.get("timezone")` always returned `None` → all schedules defaulted to UTC,
while the built-in ticker used the configured tz. Cron jobs then fired at the
wrong wall-clock time on non-UTC deployments.

**Config key confirmed:** `timezone` in `~/.hermes/config.yaml`, accessed via
`hermes_time._resolve_timezone_name()` which checks:
1. `HERMES_TIMEZONE` env var (highest priority)
2. `timezone` key in `config.yaml`
3. Empty string fallback (→ "UTC" after `or "UTC"`)

### FIX 1 — `plugins/cron_providers/temporal/schedules.py`

Replaced `job.get("timezone") or "UTC"` with:
```python
import hermes_time as _hermes_time
_configured_tz: str = _hermes_time._resolve_timezone_name() or "UTC"
```
So a `0 9 * * *` job maps to a schedule whose `time_zone` is the configured tz,
matching the built-in ticker's wall-clock interpretation.

### FIX 2 — `plugins/cron_providers/temporal/client_ops.py`

In `build_schedule` `once` branch, converted `run_at` to UTC before extracting
calendar fields:
```python
from datetime import timezone as _tz
dt = datetime.fromisoformat(spec["run_at"])
if dt.tzinfo is not None:
    dt = dt.astimezone(_tz.utc)
```
And set `time_zone_name="UTC"` explicitly (no longer uses `spec.get("time_zone")`
for once-shots). A `run_at="2026-07-01T09:00:00-04:00"` now fires at 13:00 UTC.

### New tests

**`tests/cron_providers/test_temporal_schedules.py`** — added:
- `test_cron_uses_configured_timezone`: monkeypatches `hermes_time._resolve_timezone_name` → `"America/New_York"`, asserts `spec["time_zone"] == "America/New_York"`.
- `test_cron_defaults_utc_when_unset`: resolver returns `""` → `spec["time_zone"] == "UTC"`.
- Updated `_job()` helper to drop unused `tz=` kwarg; updated `test_cron_job_maps_to_cron_spec` to use monkeypatch.

**`tests/cron_providers/test_temporal_client_ops.py`** — added:
- `test_once_honors_run_at_offset`: `run_at="2026-07-01T09:00:00-04:00"`, asserts `sched.spec.calendars[0].hour[0].start == 13` and `sched.spec.time_zone_name == "UTC"`.

### Verify outputs

```
scripts/run_tests.sh tests/cron_providers/
=== Summary: 3 files, 15 tests passed, 0 failed (100% complete) in 0.5s (24 workers) ===

ruff check plugins/cron_providers/temporal/
All checks passed!
```

---

# turso_vector final review fixes (F1–F5 + minors) — STATUS: BLOCKED (concurrent writer + F4/I3 design conflict)

Branch: feat/turso-vector-memory. Working dir worktree: .../turso-vector-memory.

## Summary
I implemented all of F1–F5 plus the minors and reached a fully green state
(41 → 42 provider tests passing, ruff clean, ty clean) at one point. While I
was still adding/verifying tests, **another agent began editing the same files
concurrently** (it added unrelated "I1/I2/I3" review fixes to the identical
modules and even rewrote my tests in place between my own tool calls). One of
its changes directly contradicts the F4 spec. I am therefore stopping and
reporting BLOCKED rather than committing into a moving, conflicting target or
clobbering the other agent's uncommitted work. Nothing has been committed.

## What I changed (all applied; present in the working tree)

### F1 — Surface memory ids in the recall block
- `plugins/memory/turso_vector/__init__.py` `_format_block` (~line 257): now emits
  `- [#{id}][{tag}] {text}{detail}`.
- `system_prompt_block` (~line 345): updated to tell the model each recalled line
  is prefixed with its id and that the id is passed to memory_rate/memory_contradict.
- Test: `tests/turso_vector_plugin/test_provider_recall.py::test_recall_block_surfaces_memory_id`
  asserts `[#<id>]` and `- [#<id>][correction]` appear in the block. PASSED.

### F2 — save_config()
- `__init__.py` `save_config()` (~line 391): writes non-secret `values` into
  config.yaml under the namespace `_load_settings` reads, via `utils.atomic_yaml_write`,
  preserving sibling keys and unrelated namespaces.
  (NOTE: the concurrent "I1" change moved that namespace from top-level
  `turso_vector` to `memory.turso_vector`; save_config currently writes
  `memory.turso_vector` to match.)
- Tests: `tests/turso_vector_plugin/test_provider_config.py` — round-trip,
  namespace-preservation. PASSED.

### F3 — Validate embedding dim in initialize() (loud, not swallowed)
- `store.py` `existing_dim()` (~line 57): parses `F32_BLOB(<dim>)` from
  sqlite_master, read BEFORE migrate().
- `__init__.py` `initialize()` (~line 132): compares embedder.dim vs configured
  embedding_dim, and existing-DB dim vs configured; on mismatch logs an
  error-level message naming both dims and sets `_enabled=False` (out of the
  generic warn/swallow path).
- Tests: `tests/turso_vector_plugin/test_provider_dim.py` — mismatch disables +
  error logged; reopen-with-different-dim surfaces error; matching dim enables.
  PASSED.

### F4 — Time-decay using the pre-update timestamp  *** CONFLICTED ***
- `store.py` `search()` now returns `last_used_at`/`created_at`;
  `decay_and_prune(..., prior_used=...)` (~line 153) decays each id by
  days_between(prior_last_used, now) — i.e. charges accumulated idle time at the
  moment of reuse, exactly as F4 specifies. `__init__.py` `prefetch` captured
  each hit's `prior_last_used` and `_decay_sweep` passed the prior map.
- Store-level test (deterministic): `test_store_lifecycle.py::test_decay_uses_prior_last_used_not_reuse_time`
  and `test_decay_without_prior_sees_zero_idle_after_mark_used`. These still pass.
- CONFLICT: the concurrent agent's "I3" change replaced the session-end path:
  `_decay_sweep` now calls a new `store.decay_stale()` (~store.py line 181) that
  sweeps ALL rows but SKIPS rows idle < 1 day. A just-reused memory has
  last_used_at=now, so I3 EXEMPTS it from decay. That is the opposite of F4
  ("decay the accumulated idle time at the moment of reuse"). The other agent
  also deleted my provider-level F4 test
  (`test_prefetch_then_session_end_decays_by_idle_time`, asserting the recalled
  memory's weight drops, w<0.5) and replaced it with
  `test_recalled_memory_not_decayed_at_session_end` asserting the OPPOSITE
  (weight == 1.0). `decay_and_prune`+`prior_used` is now dead code in the
  session-end path. Reconciling requires a design decision (F4 reuse-time decay
  vs I3 stale-sweep exemption) that I cannot make from the spec.

### F5 — Thread API-embedder endpoint keys through settings
- `__init__.py` `_DEFAULTS` (~line 56): added `embedding_api_base`
  (`https://api.openai.com/v1`) and `embedding_api_key_env`
  (`TURSO_VECTOR_EMBED_API_KEY`) so `_load_settings` passes them to make_embedder.
- Test: `tests/turso_vector_plugin/test_provider_config.py::test_api_endpoint_settings_thread_through_to_embedder`
  asserts a custom `embedding_api_base` reaches APIEmbedder.api_base. PASSED.

### Minors (applied)
- Optional type annotations for `_store`/`_embedder`/`_executor` + `assert ... is
  not None` guards on `_enabled` paths (ty clean on the file at the time).
- `memory_rate`/`memory_contradict` DB ops routed through `self._submit`.
- One-line comment on the table-wide DELETE in `decay_and_prune`.
- One-line comment in `initialize` on `get_hermes_home()` use (hermes_home kwarg
  intentionally not used for the path).
- Optional warmup: `initialize` fire-and-forgets `self._warm_embedder` on the
  executor.

## Verification snapshots
- Before the concurrent edits: `scripts/run_tests.sh tests/turso_vector_plugin/`
  → 41 tests passed, 0 failed; `tests/agent/test_db_backend_connect.py` → 4
  passed; `ruff check plugins/memory/turso_vector/` → clean; `ty check
  plugins/memory/turso_vector/` → clean.
- After the concurrent edits (current): `scripts/run_tests.sh
  tests/turso_vector_plugin/` → 2 failed in test_provider_session.py
  (`test_prefetch_then_session_end_decays_by_idle_time` and the concurrent
  agent's `test_decay_fires_for_non_recalled_old_memory` — the latter expects a
  ~542-day-idle weight-0.8 row at decay_rate 0.98 / floor 0.01 to survive, but
  0.8*0.98^542 ≈ 1e-5 < 0.01 so it is pruned: that test is independently buggy).
  The suite is a moving target — file contents changed between consecutive reads.

## Why BLOCKED (not committed)
1. Active concurrent writer on the exact files (plugins/memory/turso_vector/*.py
   and tests/turso_vector_plugin/*). Committing now would either capture the
   other agent's broken intermediate state or clobber its uncommitted edits.
2. F4 vs I3 are contradictory designs for recalled-row decay, and the other
   agent has already rewritten the F4 test to assert the opposite outcome. This
   is a design decision I cannot resolve from the spec.

## Recommended resolution (for the orchestrator)
- Serialize the two reviews (don't run F1–F5 and I1/I2/I3 in the same worktree
  simultaneously).
- Decide the decay contract: keep F4 (decay recalled rows by their prior idle at
  reuse) OR adopt I3 (exempt recently-used rows, sweep stale non-recalled rows).
  If I3 wins, remove the now-dead `decay_and_prune` + `prior_used` and my F4
  decay tests; if F4 wins, revert `_decay_sweep` to use `decay_and_prune` with
  the prior map. Also fix the I3 test `test_decay_fires_for_non_recalled_old_memory`
  (its survival assertion is arithmetically impossible at the chosen rate/floor).
