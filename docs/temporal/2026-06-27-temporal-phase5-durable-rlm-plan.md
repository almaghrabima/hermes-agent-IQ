# Temporal Phase 5 — Durable Background rlm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `rlm(durable=true)` mode that runs the rlm invocation as a crash-durable Temporal workflow on the worker and delivers the result back to the originating session via the existing Phase-2/3 outbox rail.

**Architecture:** `rlm(durable=true)` (gated on `temporal.enabled`, no silent fallback) dispatches an `RlmRunWorkflow` and returns a run_id. The workflow runs a `run_rlm_durable` activity — which simply calls the existing `rlm_tool()` sync path on the worker host — then records the result block via the existing `record_outbox` activity. The result re-enters the session through the existing drain/reconcile rail. The whole rlm run is one opaque retryable unit (bounded re-run on crash); no kernel-state checkpointing.

**Tech Stack:** Python 3.11, `temporalio==1.29.0` (optional `[temporal]` extra, lazy-imported), fast-rlm (Deno subprocess), SQLite durable outbox, pytest via `scripts/run_tests.sh`.

## Global Constraints

- **Opt-in, zero regression:** the synchronous `rlm` path is untouched; durable mode is reached only via `rlm(durable=true)`.
- **No silent fallback:** `durable=true` without `temporal.enabled` is an error (mirrors `delegate_task durable`); the plain `rlm` call still works.
- **Narrow waist:** no new core tool — one new param on the existing `rlm` tool + one workflow/activity in the existing temporal plugin; reuse the Phase-2/3 `record_outbox`/drain/reconcile rail wholesale.
- **No raw secrets in the Temporal payload:** the worker resolves rlm credentials at activity time (the activity calls `rlm_tool()`, which calls `_resolve_rlm_credentials` itself).
- **Crash policy:** bounded re-run, `maximum_attempts = rlm.durable_max_attempts` (default `2`). Full re-run from scratch; rlm's own `max_money_spent`/`max_global_calls` bound each attempt.
- **Lazy temporalio:** importing `plugins.temporal.activities`/`plugins.temporal.workflows` must not import temporalio at module top; gated e2e tests `pytest.importorskip("temporalio")`.
- **Tests use a temp `HERMES_HOME`, never the real `~/.hermes/`.** Run via `scripts/run_tests.sh` (CI parity). Never bare `pytest`.
- **Commit messages** end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Documented limitation:** the activity runs rlm with the local backend on the worker host (must have Deno + fast-rlm, + docker/KVM if configured); remote-backend durable mode and mid-run resume are non-goals.

---

## File Structure

| File | Responsibility |
|---|---|
| `plugins/temporal/activities.py` (modify) | `run_rlm_durable(payload)` activity — calls the existing `rlm_tool()` sync path on the worker, parses its JSON, returns `{ok, summary, error, usage, log_path}`. Registered in `_make_activities()`. |
| `plugins/temporal/workflows.py` (modify) | `RlmRunWorkflow` + `_make_rlm_run_workflow()` (both try/except branches) — mirrors `BackgroundDelegationWorkflow`: run `run_rlm_durable` → build block → `record_outbox` → return block+session_key. |
| `plugins/temporal/worker.py` (modify) | Register `RlmRunWorkflow` (the `run_rlm_durable` activity comes via `_make_activities()`). |
| `plugins/temporal/tools.py` (modify) | `dispatch_durable_rlm(*, rlm_args, session_key)` and `list_completed_durable_rlm()` (mirror `dispatch_durable_delegation` / `list_completed_durable_delegations`). |
| `tools/rlm_tool.py` (modify) | `durable` param on `RLM_SCHEMA` + handler + `rlm_tool()`; `"durable_max_attempts": 2` in `_RLM_CONFIG_DEFAULTS`; durable branch gates on `temporal.enabled`, resolves `session_key`, assembles `rlm_args`, calls `dispatch_durable_rlm`. |
| `plugins/temporal/delivery.py` (modify) | Extend `reconcile_from_temporal` to also backfill completed `RlmRunWorkflow`s via `list_completed_durable_rlm`. |
| `AGENTS.md` (modify) | Document `rlm(durable=true)`. |

**Tests:**
- `tests/temporal/test_rlm_durable_activity.py` (create) — `run_rlm_durable` parsing.
- `tests/temporal/test_rlm_durable_workflow.py` (create) — workflow retry config + gated e2e.
- `tests/temporal/test_rlm_durable_dispatch.py` (create) — dispatch payload/id + reconcile.
- `tests/tools/test_rlm_durable_tool.py` (create) — rlm tool durable routing + gate.

> Reference patterns to mirror (read before implementing): `BackgroundDelegationWorkflow` (`plugins/temporal/workflows.py:45-91`), `dispatch_durable_delegation` + `list_completed_durable_delegations` (`plugins/temporal/tools.py`), `reconcile_from_temporal` (`plugins/temporal/delivery.py:41-57`), `record_outbox` activity (`plugins/temporal/activities.py` `_make_activities`).

---

## Task 1: `run_rlm_durable` activity

The activity that runs rlm on the worker by calling the existing sync `rlm_tool()` and normalizing its result. Pure of Temporal in its core so it's unit-testable without a worker.

**Files:**
- Modify: `plugins/temporal/activities.py`
- Test: `tests/temporal/test_rlm_durable_activity.py`

**Interfaces:**
- Consumes: `tools.rlm_tool.rlm_tool(query, context=None, input_path=None, primary_agent=None, sub_agent=None, max_global_calls=None, task_id=None) -> str` (returns a JSON string `{"status": "success"|"error", "result"?, "usage"?, "log_path"?, "error"?}`).
- Produces: a module-level `_run_rlm_blocking(payload: dict) -> dict` returning `{"ok": bool, "summary": Any, "error": Optional[str], "usage": Any, "log_path": Optional[str]}`, and an activity `run_rlm_durable` registered in `_make_activities()` that delegates to it via `asyncio.to_thread`.

- [ ] **Step 1: Write the failing test**

```python
# tests/temporal/test_rlm_durable_activity.py
from plugins.temporal import activities as A


def test_run_rlm_blocking_success(monkeypatch):
    import tools.rlm_tool as rlm_mod
    monkeypatch.setattr(
        rlm_mod, "rlm_tool",
        lambda **kw: '{"status": "success", "result": "ANSWER", "usage": {"calls": 3}, "log_path": "/x.log"}')
    out = A._run_rlm_blocking({"rlm_args": {"query": "q"}})
    assert out["ok"] is True
    assert out["summary"] == "ANSWER"
    assert out["error"] is None
    assert out["usage"] == {"calls": 3}


def test_run_rlm_blocking_error(monkeypatch):
    import tools.rlm_tool as rlm_mod
    monkeypatch.setattr(
        rlm_mod, "rlm_tool",
        lambda **kw: '{"status": "error", "error": "boom"}')
    out = A._run_rlm_blocking({"rlm_args": {"query": "q"}})
    assert out["ok"] is False
    assert out["error"] == "boom"
    assert out["summary"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/temporal/test_rlm_durable_activity.py`
Expected: FAIL — `AttributeError: module 'plugins.temporal.activities' has no attribute '_run_rlm_blocking'`.

- [ ] **Step 3: Implement the blocking core + activity**

Add to `plugins/temporal/activities.py` (read the file first; mirror how `fire_cron_job_activity` is structured and how `_make_activities()` returns its list):

```python
def _run_rlm_blocking(payload: dict) -> dict:
    """Run rlm via the existing sync tool path on the worker host and normalize
    the result. Bootstraps tool discovery (the worker already does, but this keeps
    the core self-contained for direct unit tests). NO Temporal imports."""
    import json as _json
    from tools.registry import discover_builtin_tools
    discover_builtin_tools()
    from tools.rlm_tool import rlm_tool
    args = payload.get("rlm_args") or {}
    raw = rlm_tool(
        query=args.get("query", ""),
        context=args.get("context"),
        input_path=args.get("input_path"),
        primary_agent=args.get("primary_agent"),
        sub_agent=args.get("sub_agent"),
        max_global_calls=args.get("max_global_calls"),
        task_id=payload.get("run_id", "durable-rlm"),
    )
    try:
        parsed = _json.loads(raw)
    except Exception:  # noqa: BLE001 — non-JSON is a hard failure
        return {"ok": False, "summary": None, "error": f"rlm produced non-JSON: {raw[:500]}",
                "usage": None, "log_path": None}
    ok = parsed.get("status") == "success"
    return {
        "ok": ok,
        "summary": parsed.get("result") if ok else None,
        "error": None if ok else parsed.get("error", "rlm failed"),
        "usage": parsed.get("usage"),
        "log_path": parsed.get("log_path"),
    }
```

In `_make_activities()` (alongside the other `@activity.defn` functions), add:

```python
    @activity.defn(name="run_rlm_durable")
    async def run_rlm_durable_activity(payload: dict) -> dict:
        return await asyncio.to_thread(_run_rlm_blocking, payload)
```

and add `run_rlm_durable_activity` to the list `_make_activities()` returns.

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/temporal/test_rlm_durable_activity.py`
Expected: PASS (both).

- [ ] **Step 5: Verify lazy-import hygiene**

Run: `python -c "import plugins.temporal.activities"` (inside the venv) — must succeed without temporalio installed (the `from temporalio import activity` stays inside `_make_activities`).
Expected: no error.

- [ ] **Step 6: Commit**

```bash
git add plugins/temporal/activities.py tests/temporal/test_rlm_durable_activity.py
git commit -m "feat(temporal): run_rlm_durable activity — run rlm via the sync tool path on the worker

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `RlmRunWorkflow` + worker registration

The workflow that wraps the activity with bounded retry and records the result via the existing `record_outbox` activity. Mirrors `BackgroundDelegationWorkflow` exactly.

**Files:**
- Modify: `plugins/temporal/workflows.py`, `plugins/temporal/worker.py`
- Test: `tests/temporal/test_rlm_durable_workflow.py`

**Interfaces:**
- Consumes: `run_rlm_durable` activity (Task 1); the existing `record_outbox` activity.
- Produces: `RlmRunWorkflow` (module-level in the `try` block) + `_make_rlm_run_workflow()` in both branches; a module-level helper `_rlm_retry_policy(max_attempts)`. Payload: `{"rlm_args": dict, "session_key": str, "run_id": str, "max_attempts": int, "timeout_seconds": int}`. Returns `{"run_id", "session_key", "status", "block"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/temporal/test_rlm_durable_workflow.py
import pytest
pytest.importorskip("temporalio")

from plugins.temporal import workflows


def test_rlm_retry_policy_uses_max_attempts():
    assert workflows._rlm_retry_policy(3).maximum_attempts == 3


def test_make_rlm_run_workflow_returns_class():
    assert workflows._make_rlm_run_workflow().__name__ == "RlmRunWorkflow"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/temporal/test_rlm_durable_workflow.py`
Expected: FAIL — `AttributeError: ... has no attribute '_rlm_retry_policy'`.

- [ ] **Step 3: Implement the workflow**

In `plugins/temporal/workflows.py`, read `BackgroundDelegationWorkflow` (lines ~45-91) and mirror it. Add a retry helper and the workflow inside the `try: from temporalio import workflow as _wf ...` block:

```python
    def _rlm_retry_policy(max_attempts: int):
        return _RetryPolicy(maximum_attempts=int(max_attempts))

    @_wf.defn(name="RlmRunWorkflow")
    class RlmRunWorkflow:
        @_wf.run
        async def run(self, params: dict) -> dict:
            # params: {rlm_args, session_key, run_id, max_attempts, timeout_seconds}
            timeout_s = int(params.get("timeout_seconds", 600))
            policy = _rlm_retry_policy(int(params.get("max_attempts", 2)))
            try:
                result = await _wf.execute_activity(
                    "run_rlm_durable",
                    {"rlm_args": params.get("rlm_args") or {}, "run_id": params["run_id"]},
                    start_to_close_timeout=timedelta(seconds=timeout_s + 120),
                    retry_policy=policy,
                )
                ok = bool(result.get("ok"))
                summary = result.get("summary")
                error = None if ok else result.get("error")
            except Exception as exc:  # activity exhausted its retries
                ok, summary, error = False, None, f"durable rlm failed: {exc}"
            block = {
                "goal": (params.get("rlm_args") or {}).get("query", ""),
                "summary": summary,
                "error": error,
                "status": "completed" if ok else "failed",
            }
            await _wf.execute_activity(
                "record_outbox",
                {"run_id": params["run_id"], "session_key": params.get("session_key", "default"),
                 "status": block["status"], "block": block},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_RetryPolicy(maximum_attempts=10),
            )
            return {"run_id": params["run_id"], "session_key": params.get("session_key", "default"),
                    "status": block["status"], "block": block}
```

Add `def _make_rlm_run_workflow() -> type: return RlmRunWorkflow` in the try block (next to `_make_background_workflow`), AND a matching stub in the `except ImportError:` block that raises the same ImportError message as its siblings.

Note: `_rlm_retry_policy` lives inside the `try` block (needs `_RetryPolicy`); the Task-2 unit test is gated by `pytest.importorskip("temporalio")` so it only runs when temporalio is present.

- [ ] **Step 4: Register on the worker**

In `plugins/temporal/worker.py`, add `_make_rlm_run_workflow` to the import from `plugins.temporal.workflows` and to the `workflows=[...]` list in `run_worker` (alongside `_make_background_workflow()`). The `run_rlm_durable` activity is already in `_make_activities()` from Task 1, so `activities=_make_activities()` covers it.

- [ ] **Step 5: Run the tests**

Run: `scripts/run_tests.sh tests/temporal/test_rlm_durable_workflow.py`
Expected: PASS (both).

- [ ] **Step 6: Commit**

```bash
git add plugins/temporal/workflows.py plugins/temporal/worker.py tests/temporal/test_rlm_durable_workflow.py
git commit -m "feat(temporal): RlmRunWorkflow (mirror BackgroundDelegation) + worker registration

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `dispatch_durable_rlm` + `list_completed_durable_rlm`

The dispatch layer: start the workflow and (for reconcile) list completed runs missing from the outbox. Mirrors `dispatch_durable_delegation` / `list_completed_durable_delegations`.

**Files:**
- Modify: `plugins/temporal/tools.py`
- Test: `tests/temporal/test_rlm_durable_dispatch.py`

**Interfaces:**
- Consumes: `resolve_temporal_config`, `connect`, `_outbox` (already imported in tools.py); `RlmRunWorkflow` (by name).
- Produces: `dispatch_durable_rlm(*, rlm_args: dict, session_key: str, max_attempts: int, timeout_seconds: int) -> dict` returning `{"status": "dispatched", "run_id": str}`; `list_completed_durable_rlm() -> list[dict]` returning `[{run_id, session_key, status, block}]` for not-yet-recorded completed `RlmRunWorkflow`s.

- [ ] **Step 1: Write the failing test**

```python
# tests/temporal/test_rlm_durable_dispatch.py
import plugins.temporal.tools as T


def test_dispatch_durable_rlm_starts_workflow(monkeypatch):
    started = {}

    class FakeHandle:
        id = "durable-rlm-abc"

    class FakeClient:
        async def start_workflow(self, name, payload, *, id, task_queue):
            started["name"] = name
            started["payload"] = payload
            started["id"] = id
            return FakeHandle()

    async def fake_connect(s):
        return FakeClient()

    monkeypatch.setattr(T, "connect", fake_connect)
    out = T.dispatch_durable_rlm(
        rlm_args={"query": "q"}, session_key="sess-1", max_attempts=2, timeout_seconds=600)
    assert out["status"] == "dispatched"
    assert out["run_id"].startswith("durable-rlm-")
    assert started["name"] == "RlmRunWorkflow"
    assert started["payload"]["session_key"] == "sess-1"
    assert started["payload"]["rlm_args"] == {"query": "q"}
    assert started["payload"]["max_attempts"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/temporal/test_rlm_durable_dispatch.py`
Expected: FAIL — `AttributeError: module 'plugins.temporal.tools' has no attribute 'dispatch_durable_rlm'`.

- [ ] **Step 3: Implement dispatch + list**

Add to `plugins/temporal/tools.py` (mirror `dispatch_durable_delegation` and `list_completed_durable_delegations`):

```python
def dispatch_durable_rlm(*, rlm_args, session_key, max_attempts, timeout_seconds) -> dict:
    """Start an RlmRunWorkflow and return immediately with a run_id."""
    s = resolve_temporal_config(load_config())
    run_id = f"durable-rlm-{uuid.uuid4().hex[:12]}"

    async def _go():
        client = await connect(s)
        handle = await client.start_workflow(
            "RlmRunWorkflow",
            {"rlm_args": rlm_args, "session_key": session_key or "default",
             "run_id": run_id, "max_attempts": int(max_attempts),
             "timeout_seconds": int(timeout_seconds)},
            id=run_id, task_queue=s.task_queue,
        )
        return handle.id

    rid = asyncio.run(_go())
    return {"status": "dispatched", "run_id": rid}


def list_completed_durable_rlm() -> list[dict]:
    """Completed RlmRunWorkflows not yet in the outbox (for reconcile). Mirrors
    list_completed_durable_delegations; raises if temporal is down."""

    async def _go():
        s = resolve_temporal_config(load_config())
        client = await connect(s)
        out = []
        query = 'WorkflowType="RlmRunWorkflow" AND ExecutionStatus="Completed"'
        async for wf in client.list_workflows(query=query):
            if _outbox.has_run(wf.id):
                continue
            handle = client.get_workflow_handle(wf.id)
            res = await handle.result()
            out.append({
                "run_id": res.get("run_id", wf.id),
                "session_key": res.get("session_key", "default"),
                "status": res.get("status", "completed"),
                "block": res.get("block", {}),
            })
        return out

    return asyncio.run(_go())
```

- [ ] **Step 4: Run the test**

Run: `scripts/run_tests.sh tests/temporal/test_rlm_durable_dispatch.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/tools.py tests/temporal/test_rlm_durable_dispatch.py
git commit -m "feat(temporal): dispatch_durable_rlm + list_completed_durable_rlm

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: rlm tool `durable` param + config default

Wire the durable knob into the `rlm` tool: gate on `temporal.enabled`, resolve the session, assemble the payload, dispatch. Add the `durable_max_attempts` default.

**Files:**
- Modify: `tools/rlm_tool.py`
- Test: `tests/tools/test_rlm_durable_tool.py`

**Interfaces:**
- Consumes: `plugins.temporal.tools.dispatch_durable_rlm` (Task 3); `tools.approval.get_current_session_key`; `plugins.temporal.tconfig.resolve_temporal_config` + `hermes_cli.config.load_config` (to check `temporal.enabled`).
- Produces: `rlm_tool(..., durable: bool = False)`; `RLM_SCHEMA` gains a `durable` boolean property; `_RLM_CONFIG_DEFAULTS` gains `"durable_max_attempts": 2`.

- [ ] **Step 1: Write the failing test**

```python
# tests/tools/test_rlm_durable_tool.py
import json
import tools.rlm_tool as rlm_mod


def test_durable_requires_temporal_enabled(monkeypatch):
    # temporal disabled -> error, NO dispatch.
    monkeypatch.setattr(rlm_mod, "_temporal_enabled", lambda: False)
    called = {"n": 0}
    monkeypatch.setattr(rlm_mod, "_dispatch_durable_rlm",
                        lambda **kw: called.__setitem__("n", called["n"] + 1) or {"status": "dispatched", "run_id": "x"})
    out = json.loads(rlm_mod.rlm_tool(query="q", durable=True))
    assert out["status"] == "error"
    assert "temporal" in out["error"].lower()
    assert called["n"] == 0


def test_durable_dispatches_with_session_and_args(monkeypatch):
    monkeypatch.setattr(rlm_mod, "_temporal_enabled", lambda: True)
    monkeypatch.setattr(rlm_mod, "_current_session_key", lambda: "sess-9")
    seen = {}
    def fake_dispatch(**kw):
        seen.update(kw)
        return {"status": "dispatched", "run_id": "durable-rlm-1"}
    monkeypatch.setattr(rlm_mod, "_dispatch_durable_rlm", fake_dispatch)
    out = json.loads(rlm_mod.rlm_tool(query="big-q", context="ctx", durable=True))
    assert out["status"] == "dispatched"
    assert out["run_id"] == "durable-rlm-1"
    assert seen["session_key"] == "sess-9"
    assert seen["rlm_args"]["query"] == "big-q"
    assert seen["rlm_args"]["context"] == "ctx"
    assert seen["max_attempts"] == 2  # _RLM_CONFIG_DEFAULTS default
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_rlm_durable_tool.py`
Expected: FAIL — `TypeError: rlm_tool() got an unexpected keyword argument 'durable'` (and missing helpers).

- [ ] **Step 3: Implement the durable branch**

In `tools/rlm_tool.py`:

(a) Add the default to `_RLM_CONFIG_DEFAULTS` (the dict at the top with `timeout_seconds`/`allow_remote_backends`):

```python
    "durable_max_attempts": 2,
```

(b) Add the schema property inside `RLM_SCHEMA["parameters"]["properties"]`:

```python
            "durable": {"type": "boolean", "description": "Run as a crash-durable background Temporal workflow; returns a run_id and the result re-enters the session when done (requires temporal.enabled)."},
```

(c) Add small seam helpers near the top (so tests can monkeypatch them by name):

```python
def _temporal_enabled() -> bool:
    try:
        from plugins.temporal.tconfig import resolve_temporal_config
        from hermes_cli.config import load_config
        return bool(resolve_temporal_config(load_config()).enabled)
    except Exception:  # noqa: BLE001 — temporal not installed/configured
        return False


def _current_session_key() -> str:
    from tools.approval import get_current_session_key
    return get_current_session_key(default="default")


def _dispatch_durable_rlm(**kw) -> dict:
    from plugins.temporal.tools import dispatch_durable_rlm
    return dispatch_durable_rlm(**kw)
```

(d) Add `durable: bool = False` to the `rlm_tool` signature and a durable branch at the TOP of the `try:` (before the sync flow):

```python
def rlm_tool(query, context=None, input_path=None, primary_agent=None,
             sub_agent=None, max_global_calls=None, task_id=None, durable=False) -> str:
    try:
        _validate_context_args(context, input_path)
        if durable:
            if not _temporal_enabled():
                raise RlmError(
                    "rlm durable=true requires temporal.enabled; see docs/temporal/. "
                    "Not falling back to a non-durable run.")
            rlm_cfg = _load_rlm_config()
            rlm_args = {
                "query": query, "context": context, "input_path": input_path,
                "primary_agent": primary_agent, "sub_agent": sub_agent,
                "max_global_calls": max_global_calls,
            }
            out = _dispatch_durable_rlm(
                rlm_args=rlm_args,
                session_key=_current_session_key(),
                max_attempts=int(rlm_cfg.get("durable_max_attempts", 2)),
                timeout_seconds=int(rlm_cfg.get("timeout_seconds", 600)),
            )
            return json.dumps(out, ensure_ascii=False)
        rlm_cfg = _load_rlm_config()
        # ... existing synchronous flow unchanged ...
```

> Keep the rest of the existing synchronous body exactly as-is below the durable branch. The `rlm_cfg = _load_rlm_config()` line already exists in the sync path; the durable branch loads its own copy and returns before reaching it.

(e) Add `durable` to the registry handler lambda:

```python
    handler=lambda args, **kw: rlm_tool(
        query=args.get("query", ""),
        context=args.get("context"),
        input_path=args.get("input_path"),
        primary_agent=args.get("primary_agent"),
        sub_agent=args.get("sub_agent"),
        max_global_calls=args.get("max_global_calls"),
        task_id=kw.get("task_id"),
        durable=bool(args.get("durable", False)),
    ),
```

- [ ] **Step 4: Run the test**

Run: `scripts/run_tests.sh tests/tools/test_rlm_durable_tool.py`
Expected: PASS (both).

- [ ] **Step 5: Regression — existing rlm tool tests still pass**

Run: `scripts/run_tests.sh tests/tools/` (search first: `ls tests/tools/ | grep rlm` — run any existing rlm test files to confirm the sync path is unchanged).
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tools/rlm_tool.py tests/tools/test_rlm_durable_tool.py
git commit -m "feat(rlm): durable=true dispatches a durable Temporal rlm run (no silent fallback)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: reconcile backfill for `RlmRunWorkflow`

Extend startup reconcile to backfill rlm results completed while no consumer was alive.

**Files:**
- Modify: `plugins/temporal/delivery.py`
- Test: `tests/temporal/test_rlm_durable_dispatch.py` (append)

**Interfaces:**
- Consumes: `list_completed_durable_rlm` (Task 3); `outbox.has_run`, `outbox.record_completion` (existing).

- [ ] **Step 1: Write the failing test**

```python
# tests/temporal/test_rlm_durable_dispatch.py  (append)
def test_reconcile_backfills_rlm(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.temporal import delivery, outbox
    import plugins.temporal.tools as T

    monkeypatch.setattr(
        T, "list_completed_durable_rlm",
        lambda: [{"run_id": "durable-rlm-z", "session_key": "s", "status": "completed",
                  "block": {"goal": "q", "summary": "A", "status": "completed"}}])
    # delegation list returns nothing so only rlm is backfilled
    monkeypatch.setattr(T, "list_completed_durable_delegations", lambda: [])

    inserted = delivery.reconcile_from_temporal()
    assert inserted >= 1
    assert outbox.has_run("durable-rlm-z")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/temporal/test_rlm_durable_dispatch.py::test_reconcile_backfills_rlm`
Expected: FAIL — rlm run not backfilled (reconcile only handles delegations).

- [ ] **Step 3: Extend reconcile**

In `plugins/temporal/delivery.py` `reconcile_from_temporal`, after the existing delegation backfill loop, add an rlm loop (same shape, separate import so a failure in one doesn't block the other):

```python
    try:
        from plugins.temporal.tools import list_completed_durable_rlm
        for item in list_completed_durable_rlm():
            if not outbox.has_run(item["run_id"]):
                outbox.record_completion(item["run_id"], item["session_key"], item["status"], item["block"])
                inserted += 1
    except Exception as exc:  # best-effort
        logger.warning("temporal rlm reconcile skipped: %s", exc)
    return inserted
```

> Ensure `inserted` is initialized before the delegation loop (it is) and `return inserted` appears once at the end after both loops.

- [ ] **Step 4: Run the test**

Run: `scripts/run_tests.sh tests/temporal/test_rlm_durable_dispatch.py`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/delivery.py tests/temporal/test_rlm_durable_dispatch.py
git commit -m "feat(temporal): reconcile backfills completed RlmRunWorkflow results

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Gated end-to-end test (time-skipping)

Full loop: `RlmRunWorkflow` → `run_rlm_durable` (rlm stubbed) → `record_outbox` → result drains to the session.

**Files:**
- Test: `tests/temporal/test_rlm_durable_workflow.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1–3. Uses `temporalio.testing.WorkflowEnvironment` (mirror the existing kanban/cron time-skipping e2e in `tests/temporal/`).

- [ ] **Step 1: Write the gated e2e**

```python
# tests/temporal/test_rlm_durable_workflow.py  (append)
import pytest
pytest.importorskip("temporalio")


@pytest.mark.asyncio
async def test_rlm_workflow_runs_and_delivers(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from plugins.temporal import activities as A
    from plugins.temporal.workflows import _make_rlm_run_workflow
    from plugins.temporal import outbox, delivery
    import tools.rlm_tool as rlm_mod

    monkeypatch.setattr(
        rlm_mod, "rlm_tool",
        lambda **kw: '{"status": "success", "result": "DURABLE-ANSWER", "usage": {}, "log_path": null}'.replace("null", '""'))

    WF = _make_rlm_run_workflow()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="tq",
                          workflows=[WF], activities=A._make_activities()):
            result = await env.client.execute_workflow(
                "RlmRunWorkflow",
                {"rlm_args": {"query": "q"}, "session_key": "sess-e2e",
                 "run_id": "durable-rlm-e2e", "max_attempts": 2, "timeout_seconds": 30},
                id="durable-rlm-e2e", task_queue="tq")
    assert result["status"] == "completed"
    assert result["block"]["summary"] == "DURABLE-ANSWER"
    # It landed in the outbox and drains to the originating session.
    events = delivery.drain_outbox_for_sessions(["sess-e2e"])
    assert any(e["delegation_id"] == "durable-rlm-e2e" and e["summary"] == "DURABLE-ANSWER"
               for e in events)
```

> If `_make_activities()` needs the worker's tool registry, the activity calls `discover_builtin_tools()` itself; the stubbed `rlm_tool` short-circuits the real rlm run. Match the async-invocation style of the existing `tests/temporal/` e2e tests (asyncio marker / env usage).

- [ ] **Step 2: Run the e2e**

Run: `scripts/run_tests.sh tests/temporal/test_rlm_durable_workflow.py`
Expected: PASS (skips cleanly if `temporalio` absent).

- [ ] **Step 3: Commit**

```bash
git add tests/temporal/test_rlm_durable_workflow.py
git commit -m "test(temporal): gated e2e for RlmRunWorkflow → run_rlm_durable → outbox drain

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Documentation

Document `rlm(durable=true)` in AGENTS.md.

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Add the docs**

Find the rlm and/or Temporal sections (`grep -n "rlm\|Temporal" AGENTS.md`) and add a note near the Temporal durable-delegation bullets:

```markdown
- **Durable background rlm (Phase 5).** `rlm(durable=true)` (default off) runs the
  rlm invocation as a crash-durable `RlmRunWorkflow` on the `hermes temporal
  worker` and returns a run_id immediately; the result re-enters the originating
  session via the same durable outbox/completion rail as durable delegation
  (pollable with `durable_status`, backfilled by startup reconcile). Requires
  `temporal.enabled` — no silent fallback. On a host/worker crash the whole rlm
  run is re-run from scratch up to `rlm.durable_max_attempts` (default 2); kernel
  state is not checkpointed, and rlm's own budgets bound each attempt.
  **Limitation:** the worker runs rlm with the local backend on the worker host
  (needs Deno + fast-rlm there); durable mode over remote backends and mid-run
  resume are non-goals.
```

- [ ] **Step 2: Commit**

```bash
git add AGENTS.md
git commit -m "docs(temporal): document rlm(durable=true) (Phase 5)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **Run the touched surface:**

Run: `scripts/run_tests.sh tests/temporal/ tests/tools/`
Expected: all pass (temporal e2e skips without the extra).

- [ ] **Lint:**

Run: `ruff check tools/rlm_tool.py plugins/temporal/`
Expected: clean (only PLW1514 enabled).

- [ ] **Lazy-import sanity:** `python -c "import plugins.temporal.activities; import plugins.temporal.workflows; import tools.rlm_tool"` succeeds without temporalio installed.

- [ ] **Default-config sanity:** confirm a non-durable `rlm(query=...)` call path is unchanged (durable defaults to False).

---

## Self-Review notes (author)

**Spec coverage:** durable knob on rlm tool + gate, no silent fallback (Task 4) ✓; RlmRunWorkflow + bounded retry from `durable_max_attempts` (Task 2) ✓; activity runs rlm via the sync tool path on the worker, no secrets in payload (Task 1) ✓; result via record_outbox → drain rail (Tasks 2,6) ✓; reconcile backfill (Task 5) ✓; config default in `_RLM_CONFIG_DEFAULTS` (Task 4) ✓; worker registration (Task 2) ✓; docs + limitation (Task 7) ✓; gated e2e (Task 6) ✓; lazy-import hygiene (Tasks 1,2 verify steps) ✓.

**Type consistency:** activity returns `{ok, summary, error, usage, log_path}` (Task 1) consumed by `RlmRunWorkflow` which reads `ok`/`summary`/`error` (Task 2) ✓. `dispatch_durable_rlm(rlm_args, session_key, max_attempts, timeout_seconds)` (Task 3) called with exactly those kwargs by the rlm tool (Task 4) ✓. Block shape `{goal, summary, error, status}` (Task 2) matches what `_row_to_event` reads (`goal`/`summary`/`error`/`status`) for drain (Task 6) ✓. `list_completed_durable_rlm` returns `[{run_id, session_key, status, block}]` (Task 3) consumed by reconcile (Task 5) ✓.

**Integration checks to confirm during execution (not placeholders):** (1) confirm `record_outbox` activity exists in `_make_activities()` and its payload shape `{run_id, session_key, status, block}` before Task 2; (2) confirm the `except ImportError` stub list in workflows.py so `_make_rlm_run_workflow` has a sibling stub; (3) confirm `tests/temporal/` async e2e invocation style (asyncio marker vs explicit env) before Task 6; (4) confirm `_load_rlm_config()` merges `_RLM_CONFIG_DEFAULTS` so `durable_max_attempts` is readable via `rlm_cfg.get(...)`.
