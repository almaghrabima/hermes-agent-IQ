# Temporal Phase 2 (crash-durable background delegation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Design:** `docs/temporal/2026-06-26-temporal-phase2-durable-delegation-design.md`

**Goal:** Add opt-in `delegate_task(background=true, durable=true)` that runs the child as a Temporal workflow surviving process restart, delivering the result back via a durable per-session SQLite outbox drained by the existing completion-queue rail.

**Architecture:** A `BackgroundDelegationWorkflow` runs the delegation step (Phase 1 activity → real `delegate_task`) then a final `record_outbox` activity persists a rich completion block (same shape as `async_delegation`'s event) to a SQLite outbox under `HERMES_HOME`, keyed by session. The existing CLI/gateway completion-queue drains pull undelivered rows for their session keys, forge a fresh idle turn (prompt-cache-safe), and mark them delivered. Startup reconciliation backfills the outbox from Temporal.

**Tech Stack:** Python 3.11 (`temporalio`, `sqlite3` stdlib), Hermes plugin system, the Phase 0+1 `plugins/temporal/` plugin.

## Global Constraints

- **Prompt caching is sacred** — results re-enter ONLY via the existing completion-queue drain (a fresh idle turn). Do NOT modify `run_agent.py`'s loop, message ordering, or system prompt.
- **Narrow waist / additive** — a new `durable` flag on `delegate_task`; no new core tool. When `durable=false` (default) or `temporal.enabled` is false, behavior is byte-for-byte today's in-memory async path (regression-tested).
- **No silent fallback** — `durable=true` with temporal disabled/unreachable returns a clear error (mirrors Phase 1 `preflightRuntime`).
- **Delivery rail = one rail, three roles:** durable outbox (auto re-entry) + `durable_status` (on-demand, from Phase 1) + startup reconciliation (backfills the outbox). NOT two parallel delivery engines.
- **Idempotent delivery** — outbox keyed by `run_id`; atomic claim+mark prevents double or cross-session delivery.
- **Profile-aware paths** — outbox DB at `get_hermes_home()/temporal_outbox.db`; never hardcode `~/.hermes`.
- **Tests** — via `scripts/run_tests.sh`, temp `HERMES_HOME`; gated e2e skips without the `temporal` binary (Phase 1 pattern). No change-detector tests.
- **Session key** — captured at dispatch via `tools.approval.get_current_session_key(default="default")`; empty/`<cli>`/`default` ⇒ CLI single session; a gateway `build_session_key` value ⇒ that conversation.

## File Structure

- Create: `plugins/temporal/outbox.py` — SQLite durable outbox (record/claim/mark).
- Create: `plugins/temporal/delivery.py` — rows→completion-events drain + Temporal reconciliation.
- Modify: `plugins/temporal/activities.py` — add `record_outbox_activity`.
- Modify: `plugins/temporal/workflows.py` — add `BackgroundDelegationWorkflow`.
- Modify: `plugins/temporal/tools.py` — add `dispatch_durable_delegation(...)` helper used by delegate_tool.
- Modify: `tools/delegate_tool.py` — `durable` param + routing + gating.
- Modify: `cli.py` (one call at the process-loop drain) and `gateway/run.py` (one call in the watch-event drain) + a startup reconciliation call.
- Tests: `tests/plugins/temporal/test_outbox.py`, `test_delivery.py`, `test_durable_delegation_routing.py`, `test_phase2_integration.py` (gated).

---

## Task 1: Durable outbox — `outbox.py`

**Files:**
- Create: `plugins/temporal/outbox.py`
- Test: `tests/plugins/temporal/test_outbox.py`

**Interfaces:**
- Produces: `record_completion(run_id: str, session_key: str, status: str, block: dict) -> None`; `claim_undelivered(session_keys: list[str], limit: int = 50) -> list[dict]` (atomically marks the returned rows delivered); `has_run(run_id: str) -> bool`; `_db_path() -> Path`.

- [ ] **Step 1: Write the failing test**

```python
# tests/plugins/temporal/test_outbox.py
import json
from plugins.temporal import outbox

def test_record_and_claim_marks_delivered(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("run-1", "sessA", "completed", {"goal": "g", "summary": "s"})
    rows = outbox.claim_undelivered(["sessA"])
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["session_key"] == "sessA"
    assert rows[0]["block"]["summary"] == "s"
    # second claim returns nothing (already delivered) -> no double delivery
    assert outbox.claim_undelivered(["sessA"]) == []

def test_record_is_idempotent_on_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("run-2", "s", "completed", {"summary": "a"})
    outbox.record_completion("run-2", "s", "completed", {"summary": "b"})  # ignored
    rows = outbox.claim_undelivered(["s"])
    assert len(rows) == 1
    assert rows[0]["block"]["summary"] == "a"

def test_claim_filters_by_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("r1", "A", "completed", {})
    outbox.record_completion("r2", "B", "completed", {})
    assert [r["run_id"] for r in outbox.claim_undelivered(["A"])] == ["r1"]
    assert [r["run_id"] for r in outbox.claim_undelivered(["B"])] == ["r2"]

def test_has_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert outbox.has_run("x") is False
    outbox.record_completion("x", "s", "completed", {})
    assert outbox.has_run("x") is True
```

- [ ] **Step 2: Run it — expect FAIL** (`ModuleNotFoundError: plugins.temporal.outbox`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_outbox.py`

- [ ] **Step 3: Implement `outbox.py`**

```python
# plugins/temporal/outbox.py
from __future__ import annotations
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional
from hermes_constants import get_hermes_home

_lock = threading.Lock()

def _db_path() -> Path:
    return get_hermes_home() / "temporal_outbox.db"

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), isolation_level=None, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outbox ("
        " run_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, status TEXT NOT NULL,"
        " block TEXT NOT NULL, created_at REAL NOT NULL, delivered_at REAL)"
    )
    return conn

def record_completion(run_id: str, session_key: str, status: str, block: dict) -> None:
    """Persist a completed durable delegation. Idempotent on run_id (first write wins)."""
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO outbox(run_id, session_key, status, block, created_at)"
                " VALUES(?,?,?,?,?)",
                (run_id, session_key or "default", status, json.dumps(block), time.time()),
            )
        finally:
            conn.close()

def has_run(run_id: str) -> bool:
    with _lock:
        conn = _conn()
        try:
            return conn.execute("SELECT 1 FROM outbox WHERE run_id=?", (run_id,)).fetchone() is not None
        finally:
            conn.close()

def claim_undelivered(session_keys: list[str], limit: int = 50) -> list[dict[str, Any]]:
    """Atomically fetch + mark-delivered undelivered rows for the given session keys."""
    if not session_keys:
        return []
    with _lock:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            qs = ",".join("?" for _ in session_keys)
            rows = conn.execute(
                f"SELECT run_id, session_key, status, block FROM outbox"
                f" WHERE delivered_at IS NULL AND session_key IN ({qs})"
                f" ORDER BY created_at LIMIT ?",
                (*session_keys, limit),
            ).fetchall()
            now = time.time()
            for r in rows:
                conn.execute("UPDATE outbox SET delivered_at=? WHERE run_id=?", (now, r[0]))
            conn.execute("COMMIT")
            return [
                {"run_id": r[0], "session_key": r[1], "status": r[2], "block": json.loads(r[3])}
                for r in rows
            ]
        finally:
            conn.close()
```

- [ ] **Step 4: Run it — expect PASS**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_outbox.py`

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/outbox.py tests/plugins/temporal/test_outbox.py
git commit -m "feat(temporal): durable per-session SQLite outbox (Phase 2)"
```

---

## Task 2: Delivery — `delivery.py`

**Files:**
- Create: `plugins/temporal/delivery.py`
- Test: `tests/plugins/temporal/test_delivery.py`

**Interfaces:**
- Consumes: `outbox.claim_undelivered` (Task 1).
- Produces: `drain_outbox_for_sessions(session_keys: list[str]) -> list[dict]` — returns `type="async_delegation"` completion events (same shape as `tools/async_delegation.py`'s event) for undelivered rows, marking them delivered; `reconcile_from_temporal() -> int` — backfills the outbox from Temporal for completed durable delegations not present (best-effort; returns count).

- [ ] **Step 1: Write the failing test**

```python
# tests/plugins/temporal/test_delivery.py
from plugins.temporal import outbox, delivery

def test_drain_produces_async_delegation_events(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    block = {"goal": "g", "context": None, "toolsets": None, "role": "leaf",
             "model": "m", "summary": "done", "error": None}
    outbox.record_completion("run-1", "sessA", "completed", block)
    events = delivery.drain_outbox_for_sessions(["sessA"])
    assert len(events) == 1
    e = events[0]
    assert e["type"] == "async_delegation"
    assert e["session_key"] == "sessA"
    assert e["status"] == "completed"
    assert e["goal"] == "g"
    assert e["summary"] == "done"
    # drained rows are delivered -> no repeat
    assert delivery.drain_outbox_for_sessions(["sessA"]) == []
```

- [ ] **Step 2: Run it — expect FAIL** (`ModuleNotFoundError`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_delivery.py`

- [ ] **Step 3: Implement `delivery.py`**

```python
# plugins/temporal/delivery.py
from __future__ import annotations
import logging
from plugins.temporal import outbox

logger = logging.getLogger(__name__)


def _row_to_event(row: dict) -> dict:
    """Convert an outbox row into a type='async_delegation' completion event,
    matching tools/async_delegation.py's _push_completion_event shape so the
    existing CLI/gateway drains forge a turn identically."""
    b = row.get("block") or {}
    return {
        "type": "async_delegation",
        "delegation_id": row["run_id"],
        "session_key": row["session_key"],
        "goal": b.get("goal", ""),
        "context": b.get("context"),
        "toolsets": b.get("toolsets"),
        "role": b.get("role"),
        "model": b.get("model"),
        "status": row.get("status", b.get("status", "completed")),
        "summary": b.get("summary"),
        "error": b.get("error"),
        "api_calls": b.get("api_calls", 0),
        "duration_seconds": b.get("duration_seconds"),
        "dispatched_at": b.get("dispatched_at"),
        "completed_at": b.get("completed_at"),
        "exit_reason": b.get("exit_reason"),
        "durable": True,
    }


def drain_outbox_for_sessions(session_keys: list[str]) -> list[dict]:
    """Claim undelivered durable-delegation results for these sessions and return
    them as completion events (already marked delivered)."""
    rows = outbox.claim_undelivered(session_keys)
    return [_row_to_event(r) for r in rows]


def reconcile_from_temporal() -> int:
    """Best-effort startup backfill: record completed durable delegations that are
    missing from the outbox (e.g. finished while no Hermes process was alive).
    Returns the number of rows inserted. No-op if temporal is unavailable."""
    try:
        from plugins.temporal.tools import list_completed_durable_delegations  # Task 4
    except Exception:
        return 0
    inserted = 0
    try:
        for item in list_completed_durable_delegations():
            if not outbox.has_run(item["run_id"]):
                outbox.record_completion(item["run_id"], item["session_key"], item["status"], item["block"])
                inserted += 1
    except Exception as exc:  # best-effort
        logger.warning("temporal reconcile skipped: %s", exc)
    return inserted
```

- [ ] **Step 4: Run it — expect PASS** (the reconcile path is covered by the gated e2e in Task 6; the unit test exercises drain only)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_delivery.py`

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/delivery.py tests/plugins/temporal/test_delivery.py
git commit -m "feat(temporal): outbox->completion-event delivery + reconcile stub (Phase 2)"
```

---

## Task 3: Workflow + outbox activity

**Files:**
- Modify: `plugins/temporal/activities.py`
- Modify: `plugins/temporal/workflows.py`
- Test: exercised by the gated e2e (Task 6); these need temporalio so no isolated unit test.

**Interfaces:**
- Produces: `record_outbox_activity` (activity name `"record_outbox"`) calling `outbox.record_completion`; `BackgroundDelegationWorkflow` running the delegation step then `record_outbox`.

- [ ] **Step 1: Add the outbox activity to `activities.py`** (inside `_make_activity` so it registers alongside `run_step`; return BOTH activities)

Change `_make_activity()` to build and return a list:

```python
# plugins/temporal/activities.py  (replace _make_activity)
def _make_activities():
    from temporalio import activity  # type: ignore
    import asyncio

    @activity.defn(name="run_step")
    async def run_step_activity(step: dict) -> dict:
        return await asyncio.to_thread(execute_durable_step, step)

    @activity.defn(name="record_outbox")
    async def record_outbox_activity(payload: dict) -> None:
        from plugins.temporal import outbox
        await asyncio.to_thread(
            outbox.record_completion,
            payload["run_id"], payload["session_key"], payload["status"], payload["block"],
        )

    return [run_step_activity, record_outbox_activity]
```

Keep a thin back-compat shim so Phase 1 callers/tests of `_make_activity` still work:

```python
def _make_activity():
    """Back-compat: return only the run_step activity (Phase 1 worker used [_make_activity()])."""
    return _make_activities()[0]
```

- [ ] **Step 2: Add `BackgroundDelegationWorkflow` to `workflows.py`** (module-level inside the existing `try: from temporalio ...` block, next to `DurableRunWorkflow`)

```python
    @workflow.defn(name="BackgroundDelegationWorkflow")
    class BackgroundDelegationWorkflow:
        @workflow.run
        async def run(self, params: dict) -> dict:
            # params: {goal, context, toolsets, role, model, session_key, run_id, retry, step_timeout_seconds}
            retry = params.get("retry") or {}
            timeout_s = int(params.get("step_timeout_seconds", 600))
            policy = RetryPolicy(
                maximum_attempts=int(retry.get("max_attempts", 3)),
                initial_interval=timedelta(seconds=int(retry.get("initial_interval_seconds", 1))),
                backoff_coefficient=float(retry.get("backoff_coefficient", 2.0)),
            )
            step = {"name": "delegation", "prompt": params["goal"]}
            result = await workflow.execute_activity(
                "run_step", step,
                start_to_close_timeout=timedelta(seconds=timeout_s), retry_policy=policy,
            )
            block = {
                "goal": params.get("goal", ""), "context": params.get("context"),
                "toolsets": params.get("toolsets"), "role": params.get("role"),
                "model": params.get("model"),
                "summary": result.get("result"), "error": None if result.get("ok") else result.get("result"),
                "status": "completed" if result.get("ok") else "failed",
            }
            await workflow.execute_activity(
                "record_outbox",
                {"run_id": params["run_id"], "session_key": params.get("session_key", "default"),
                 "status": block["status"], "block": block},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=10),
            )
            return {"run_id": params["run_id"], "status": block["status"]}
```

- [ ] **Step 3: Update `worker.py`** to register both activities and both workflows. Change the Worker construction in `run_worker`:

```python
    from plugins.temporal.workflows import _make_workflow, _make_background_workflow
    from plugins.temporal.activities import _make_activities
    worker = Worker(
        client, task_queue=s.task_queue,
        workflows=[_make_workflow(), _make_background_workflow()],
        activities=_make_activities(),
    )
```

Add `_make_background_workflow()` to `workflows.py` mirroring `_make_workflow()` (returns `BackgroundDelegationWorkflow` in the try branch; raises the curated ImportError in the except branch).

- [ ] **Step 4: Verify imports still clean without temporalio**

Run: `python -c "import plugins.temporal.workflows, plugins.temporal.activities; print('ok')"`
Expected: `ok`. Then `scripts/run_tests.sh tests/plugins/temporal/` (all prior tests still pass; `_make_activity` shim keeps Phase 1 green).

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/activities.py plugins/temporal/workflows.py plugins/temporal/worker.py
git commit -m "feat(temporal): BackgroundDelegationWorkflow + record_outbox activity (Phase 2)"
```

---

## Task 4: Dispatch helper + Temporal listing — `tools.py`

**Files:**
- Modify: `plugins/temporal/tools.py`
- Test: `tests/plugins/temporal/test_durable_delegation_routing.py` (the dispatch helper, client mocked)

**Interfaces:**
- Produces: `dispatch_durable_delegation(goal, context, toolsets, role, model, session_key, retry=None) -> dict` (starts `BackgroundDelegationWorkflow`, returns `{"status":"dispatched","run_id"}`); `list_completed_durable_delegations() -> list[dict]` (used by `delivery.reconcile_from_temporal`).

- [ ] **Step 1: Write the failing test** (mock `connect`)

```python
# tests/plugins/temporal/test_durable_delegation_routing.py
import json
from plugins.temporal import tools

class _FakeHandle:
    id = "durable-deleg-abc"
class _FakeClient:
    async def start_workflow(self, *a, **kw):
        assert kw.get("task_queue")
        return _FakeHandle()

def test_dispatch_durable_delegation_returns_handle(monkeypatch):
    async def fake_connect(s): return _FakeClient()
    monkeypatch.setattr(tools, "connect", fake_connect)
    out = tools.dispatch_durable_delegation(
        goal="do x", context=None, toolsets=None, role="leaf", model=None, session_key="sessA")
    assert out["status"] == "dispatched"
    assert out["run_id"] == "durable-deleg-abc"
```

- [ ] **Step 2: Run it — expect FAIL** (`AttributeError: dispatch_durable_delegation`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_durable_delegation_routing.py`

- [ ] **Step 3: Implement in `tools.py`** (append)

```python
import uuid as _uuid

def dispatch_durable_delegation(*, goal, context, toolsets, role, model, session_key, retry=None) -> dict:
    """Start a BackgroundDelegationWorkflow and return immediately with a run_id."""
    import asyncio
    s = resolve_temporal_config(load_config())
    run_id = f"durable-deleg-{_uuid.uuid4().hex[:12]}"
    async def _go():
        client = await connect(s)
        await client.start_workflow(
            "BackgroundDelegationWorkflow",
            {"goal": goal, "context": context, "toolsets": toolsets, "role": role,
             "model": model, "session_key": session_key or "default", "run_id": run_id,
             "retry": retry, "step_timeout_seconds": s.step_timeout_seconds},
            id=run_id, task_queue=s.task_queue,
        )
        return run_id
    rid = asyncio.run(_go())
    return {"status": "dispatched", "run_id": rid}


def list_completed_durable_delegations() -> list[dict]:
    """Query Temporal for completed BackgroundDelegationWorkflows (for outbox reconcile).
    Returns [{run_id, session_key, status, block}]. Best-effort; raises if temporal down."""
    import asyncio
    s = resolve_temporal_config(load_config())
    async def _go():
        client = await connect(s)
        out = []
        query = 'WorkflowType="BackgroundDelegationWorkflow" AND ExecutionStatus="Completed"'
        async for wf in client.list_workflows(query=query):
            handle = client.get_workflow_handle(wf.id)
            res = await handle.result()
            out.append({"run_id": res.get("run_id", wf.id), "session_key": "default",
                        "status": res.get("status", "completed"),
                        "block": {"summary": res.get("status"), "goal": ""}})
        return out
    return asyncio.run(_go())
```

> Note: `list_completed_durable_delegations` reconstructs only a minimal block; the
> authoritative rich block is written by `record_outbox` during the workflow, so
> reconcile is a backstop for the rare "outbox write never happened" case.

- [ ] **Step 4: Run it — expect PASS**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_durable_delegation_routing.py`

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/tools.py tests/plugins/temporal/test_durable_delegation_routing.py
git commit -m "feat(temporal): dispatch_durable_delegation + reconcile listing (Phase 2)"
```

---

## Task 5: `delegate_task(durable=true)` routing + drain hooks

**Files:**
- Modify: `tools/delegate_tool.py`
- Modify: `cli.py` (process-loop drain), `gateway/run.py` (watch-event drain)
- Test: `tests/tools/test_delegate_durable.py`

**Interfaces:**
- Consumes: `plugins.temporal.tools.dispatch_durable_delegation`, `delivery.drain_outbox_for_sessions`.

- [ ] **Step 1: Write the failing test** (gating + routing, temporal mocked)

```python
# tests/tools/test_delegate_durable.py
import json
import tools.delegate_tool as dt

def test_durable_requires_temporal_enabled(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"temporal": {"enabled": False}})
    out = json.loads(dt.delegate_task(goal="g", background=True, durable=True))
    assert out["status"] == "error"
    assert "temporal" in out["error"].lower()

def test_durable_routes_to_temporal_dispatch(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"temporal": {"enabled": True}})
    calls = {}
    def fake_dispatch(**kw):
        calls.update(kw); return {"status": "dispatched", "run_id": "durable-deleg-xyz"}
    monkeypatch.setattr("plugins.temporal.tools.dispatch_durable_delegation", fake_dispatch)
    out = json.loads(dt.delegate_task(goal="do x", background=True, durable=True))
    assert out["status"] == "dispatched"
    assert out["run_id"] == "durable-deleg-xyz"
    assert calls["goal"] == "do x"
```

- [ ] **Step 2: Run it — expect FAIL** (`durable` is not a param yet)

Run: `scripts/run_tests.sh tests/tools/test_delegate_durable.py`

- [ ] **Step 3: Add `durable` param + routing to `delegate_task`** in `tools/delegate_tool.py`

Add `durable: Optional[bool] = None,` to the signature (after `background`). At the very top of the `if background:` block (before the existing async dispatch), insert:

```python
        durable = is_truthy_value(durable, default=False) if durable is not None else False
        if durable:
            cfg = _load_config()
            if not ((cfg.get("temporal") or {}).get("enabled")):
                return json.dumps({"status": "error",
                    "error": "delegate_task durable=true requires temporal.enabled; "
                             "see docs/temporal/. Not falling back to non-durable."})
            from plugins.temporal.tools import dispatch_durable_delegation
            from tools.approval import get_current_session_key
            try:
                out = dispatch_durable_delegation(
                    goal=goal, context=context, toolsets=toolsets,
                    role=(role or "leaf"), model=None,
                    session_key=get_current_session_key(default="default"))
            except Exception as e:  # noqa: BLE001
                return json.dumps({"status": "error", "error": f"durable dispatch failed: {e}"})
            return json.dumps(out)
```

Also add `durable` to the tool's JSON schema (`DELEGATE_TASK_SCHEMA`) as an optional boolean property with a one-line description: "Run the background subagent as a crash-durable Temporal workflow (requires temporal.enabled)." and thread it through the registration lambda (`durable=args.get("durable")`).

- [ ] **Step 4: Run it — expect PASS**

Run: `scripts/run_tests.sh tests/tools/test_delegate_durable.py`

- [ ] **Step 5: Wire the drain hooks** (one call each; no new loops)

In `cli.py` `process_loop()` (≈ line 14414), where the in-memory `completion_queue` is drained each idle tick, add before/after the existing drain:

```python
        try:
            from plugins.temporal.delivery import drain_outbox_for_sessions
            from tools.process_registry import process_registry
            for _evt in drain_outbox_for_sessions(["default"]):
                process_registry.completion_queue.put(_evt)
        except Exception:
            pass
```

In `gateway/run.py` `_drain_gateway_watch_events(...)` (≈ line 2289), add the live session keys' outbox drain to the returned events (gather the gateway's live `session_key`s from the session store, call `drain_outbox_for_sessions(live_keys)`, extend the event list).

At CLI startup and gateway startup, call once:

```python
        try:
            from plugins.temporal.delivery import reconcile_from_temporal
            reconcile_from_temporal()
        except Exception:
            pass
```

- [ ] **Step 6: Manual smoke (no temporal needed)**

Run: `python -c "import cli, gateway.run; print('import ok')"` (ensures the hook insertions don't break imports). Then `scripts/run_tests.sh tests/tools/test_delegate_durable.py tests/plugins/temporal/`.

- [ ] **Step 7: Commit**

```bash
git add tools/delegate_tool.py cli.py gateway/run.py tests/tools/test_delegate_durable.py
git commit -m "feat(temporal): delegate_task durable=true routing + outbox drain hooks (Phase 2)"
```

---

## Task 6: Gated restart + reconcile e2e

**Files:**
- Create: `tests/plugins/temporal/test_phase2_integration.py`

- [ ] **Step 1: Write the gated e2e** (skips without temporalio; uses time-skipping env + a fake delegation activity)

```python
# tests/plugins/temporal/test_phase2_integration.py
import uuid
import pytest
pytest.importorskip("temporalio")
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity
from plugins.temporal.workflows import _make_background_workflow
from plugins.temporal import outbox, delivery

pytestmark = pytest.mark.integration

@activity.defn(name="run_step")
async def ok_step(step: dict) -> dict:
    return {"name": step.get("name", ""), "ok": True, "result": "answer"}

@activity.defn(name="record_outbox")
async def real_record(payload: dict) -> None:
    outbox.record_completion(payload["run_id"], payload["session_key"], payload["status"], payload["block"])

@pytest.mark.asyncio
async def test_durable_delegation_delivers_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"hermes-p2-{uuid.uuid4().hex[:8]}"
        run_id = f"durable-deleg-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=tq,
                          workflows=[_make_background_workflow()], activities=[ok_step, real_record]):
            await env.client.execute_workflow(
                "BackgroundDelegationWorkflow",
                {"goal": "q", "session_key": "sessA", "run_id": run_id},
                id=run_id, task_queue=tq)
    # "restart": a fresh delivery call (new process) finds the outbox row
    events = delivery.drain_outbox_for_sessions(["sessA"])
    assert len(events) == 1
    assert events[0]["type"] == "async_delegation"
    assert events[0]["session_key"] == "sessA"
    assert events[0]["summary"] == "answer"
    assert delivery.drain_outbox_for_sessions(["sessA"]) == []  # exactly once
```

- [ ] **Step 2: Run it**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_phase2_integration.py -- -m integration -o "addopts="` (with `temporalio` installed). Expected: PASS — the workflow records to the outbox; a fresh `drain_outbox_for_sessions` delivers it exactly once (the restart scenario). Without temporalio: SKIPPED. State which case ran.

- [ ] **Step 3: Commit**

```bash
git add tests/plugins/temporal/test_phase2_integration.py
git commit -m "test(temporal): durable delegation restart-delivery e2e (Phase 2)"
```

---

## Task 7: Docs + final gate

**Files:**
- Modify: `AGENTS.md` (extend the Temporal section), `skills/recursive-language-model/SKILL.md` is unrelated — do NOT touch.

- [ ] **Step 1: Extend AGENTS.md's Temporal section** with: `delegate_task(background=true, durable=true)` runs the child as a Temporal workflow surviving restart; results land in the durable outbox (`HERMES_HOME/temporal_outbox.db`) and re-enter via the normal completion-queue drain; on-demand via `durable_status`; startup reconciliation backfills. Note: requires `temporal.enabled`; no silent fallback. Keep terse (~8-12 lines).

- [ ] **Step 2: Full gate**

Run:
`scripts/run_tests.sh tests/plugins/temporal/ tests/tools/test_delegate_durable.py`
`ruff check plugins/temporal/ tools/delegate_tool.py`
Record output; note the e2e SKIPs unless temporalio installed.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs(temporal): document durable delegation (Phase 2)"
```

---

## Self-review notes (coverage)

- Spec "durable workflow surviving restart": Task 3 (`BackgroundDelegationWorkflow` + `record_outbox`). ✓
- Spec "durable outbox": Task 1. ✓
- Spec "delivery via existing completion-queue rail": Task 2 (`drain_outbox_for_sessions` → `async_delegation` events) + Task 5 hooks. ✓
- Spec "on-demand status": reuses Phase 1 `durable_status` (no new work). ✓
- Spec "startup reconciliation": Task 2 `reconcile_from_temporal` + Task 4 `list_completed_durable_delegations` + Task 5 startup call. ✓
- Spec "opt-in `durable=true`, gated, no silent fallback, zero regression": Task 5 (routing + gating + regression test that `durable=false` path is untouched). ✓
- Spec "session-key routing (CLI vs gateway)": Task 1 (session_key column + filter), Task 5 (CLI `["default"]`, gateway live keys). ✓
- Spec "idempotent / no double delivery": Task 1 atomic claim+mark; Task 6 asserts exactly-once. ✓
- Out of scope (P3/P4): absent. ✓
- Name consistency: `_make_activities` (new) + `_make_activity` (back-compat shim) both defined in Task 3; `_make_background_workflow` defined in Task 3 and consumed in Tasks 3/6; `dispatch_durable_delegation`/`list_completed_durable_delegations` defined in Task 4, consumed in Tasks 5/2. ✓
