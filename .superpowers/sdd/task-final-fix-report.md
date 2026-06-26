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
