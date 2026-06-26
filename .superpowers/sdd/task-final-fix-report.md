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
