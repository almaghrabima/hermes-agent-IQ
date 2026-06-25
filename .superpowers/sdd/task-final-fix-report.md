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
