# Temporal Phase 3 (human-in-the-loop) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Design:** `docs/temporal/2026-06-26-temporal-phase3-human-in-the-loop-design.md`

**Goal:** Add opt-in `durable_ask(prompt, choices?, timeout_seconds?)` — a durable Temporal workflow that pauses on a signal until a human runs `hermes temporal respond <run_id> "<answer>"` (CLI) or `/respond` (gateway), then resumes and delivers the answer via the Phase 2 outbox rail.

**Architecture:** A `HumanInputWorkflow` registers a `respond` signal handler and `await workflow.wait_condition(answered, timeout=timer)`; on signal it completes `{status:"answered", answer}`, on timer expiry `{status:"timed_out"}`, then calls the Phase-2 `record_outbox` activity so the result re-enters via the existing completion-queue drain. `durable_ask` starts it and writes a durable "waiting" outbox notice; `respond` (CLI subcommand + gateway slash command) signals it, authorized against the originating session_key.

**Tech Stack:** Python 3.11 (`temporalio`, sqlite3 stdlib), the Phase 0+1/2 `plugins/temporal/` plugin.

## Global Constraints

- **Prompt caching is sacred** — the "waiting" notice and the answer re-enter ONLY via the existing completion-queue drain (Phase 2 rail). Do NOT modify the agent loop or system prompt.
- **Narrow waist / additive** — `durable_ask` is a `check_fn`-gated plugin tool; `/respond` a plugin slash command; no core tool. The existing `tools/approval.py` / `/approve` system is UNTOUCHED.
- **No silent fallback** — `durable_ask` with `temporal.enabled` false → clear error (mirrors P1/P2).
- **Timeout** — `timeout_seconds` default **86400** (1 day); on expiry → `status="timed_out"`, no answer. No auto-default-response.
- **Authorization** — `respond` is restricted to the originating `session_key`, sourced from the outbox `<run_id>:waiting` row; reject a mismatch.
- **Outbox reuse** — the waiting notice is an ordinary `record_completion` row with `status="waiting"` and run_id `<run_id>:waiting` (distinct from the answer row `<run_id>`); no outbox schema change.
- **Lazy temporalio** — all temporalio imports inside `_make_*`/`connect`; importing plugin modules must not require temporalio.
- **Tests** — via `scripts/run_tests.sh`, temp `HERMES_HOME`; gated e2e skips without the `temporal` binary (temporalio==1.29.0 is installed in `.venv`).

## File Structure

- Modify: `plugins/temporal/workflows.py` — `HumanInputWorkflow` + `_make_human_input_workflow()`.
- Modify: `plugins/temporal/worker.py` — register the new workflow.
- Modify: `plugins/temporal/tools.py` — `dispatch_human_input`, `signal_human_input`, `DURABLE_ASK_SCHEMA`, `handle_durable_ask`; extend `handle_durable_status` for the waiting state.
- Modify: `plugins/temporal/outbox.py` — add `get_row(run_id) -> dict | None` (read a single row, for authz + waiting-state lookup).
- Modify: `plugins/temporal/__init__.py` — register `durable_ask` tool + `/respond` slash command; add a `respond` CLI subcommand to the existing `temporal` command.
- Modify: `plugins/temporal/worker.py` — `setup_respond_parser` + `cmd_temporal_respond`; route the temporal CLI command by `temporal_command`.
- Tests: `tests/plugins/temporal/test_durable_ask.py`, `test_human_input_authz.py`, `test_phase3_integration.py` (gated).

---

## Task 1: `outbox.get_row` helper

**Files:**
- Modify: `plugins/temporal/outbox.py`
- Test: `tests/plugins/temporal/test_outbox_get_row.py`

**Interfaces:**
- Produces: `get_row(run_id: str) -> dict | None` returning `{run_id, session_key, status, block, delivered_at}` or None.

- [ ] **Step 1: Write the failing test**

```python
# tests/plugins/temporal/test_outbox_get_row.py
from plugins.temporal import outbox

def test_get_row_returns_record(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("run-9:waiting", "sessA", "waiting", {"prompt": "ok?"})
    row = outbox.get_row("run-9:waiting")
    assert row["session_key"] == "sessA"
    assert row["status"] == "waiting"
    assert row["block"]["prompt"] == "ok?"

def test_get_row_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert outbox.get_row("nope") is None
```

- [ ] **Step 2: Run it — expect FAIL** (`AttributeError: get_row`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_outbox_get_row.py`

- [ ] **Step 3: Implement `get_row` in `outbox.py`** (append, mirroring `has_run`'s connection pattern)

```python
def get_row(run_id: str) -> dict | None:
    with _lock:
        conn = _conn()
        try:
            r = conn.execute(
                "SELECT run_id, session_key, status, block, delivered_at FROM outbox WHERE run_id=?",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()
    if r is None:
        return None
    return {"run_id": r[0], "session_key": r[1], "status": r[2],
            "block": json.loads(r[3]), "delivered_at": r[4]}
```

- [ ] **Step 4: Run it — expect PASS**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_outbox_get_row.py`

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/outbox.py tests/plugins/temporal/test_outbox_get_row.py
git commit -m "feat(temporal): outbox.get_row helper (Phase 3)"
```

---

## Task 2: `HumanInputWorkflow` + worker registration

**Files:**
- Modify: `plugins/temporal/workflows.py`
- Modify: `plugins/temporal/worker.py`
- Test: exercised by the gated e2e (Task 5); needs temporalio.

**Interfaces:**
- Produces: `HumanInputWorkflow` (signal `respond`), `_make_human_input_workflow()`.

- [ ] **Step 1: Add `HumanInputWorkflow` to `workflows.py`** (module-level, inside the existing `try: from temporalio import workflow as _wf ...` block, next to the other workflows)

```python
    @workflow.defn(name="HumanInputWorkflow")
    class HumanInputWorkflow:
        def __init__(self) -> None:
            self._answer = None
            self._answered = False

        @workflow.signal(name="respond")
        def respond(self, answer: str) -> None:
            if not self._answered:
                self._answer = answer
                self._answered = True

        @workflow.query(name="get_session_key")
        def get_session_key(self) -> str:
            return self._session_key  # set in run()

        @workflow.run
        async def run(self, params: dict) -> dict:
            import asyncio as _asyncio
            self._session_key = params.get("session_key", "default")
            timeout_s = int(params.get("timeout_seconds", 86400))
            try:
                await workflow.wait_condition(lambda: self._answered, timeout=timedelta(seconds=timeout_s))
                status, answer = "answered", self._answer
            except _asyncio.TimeoutError:
                status, answer = "timed_out", None
            block = {
                "goal": params.get("prompt", ""), "context": params.get("context"),
                "toolsets": None, "role": None, "model": None,
                "summary": answer, "error": None,
                "status": status,
            }
            await workflow.execute_activity(
                "record_outbox",
                {"run_id": params["run_id"], "session_key": self._session_key,
                 "status": status, "block": block},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=10),
            )
            return {"run_id": params["run_id"], "session_key": self._session_key,
                    "status": status, "block": block}
```

- [ ] **Step 2: Add `_make_human_input_workflow()`** to `workflows.py` mirroring `_make_background_workflow()` (return the class in the `try` branch; in the `except ImportError` branch define a function that raises the curated ImportError):

In the `try` branch (with the other `_make_*`):
```python
    def _make_human_input_workflow() -> type:
        return HumanInputWorkflow
```
In the `except ImportError` branch (with the other shims):
```python
    def _make_human_input_workflow() -> type:  # type: ignore[misc]
        raise ImportError(
            "temporalio is required for the durable orchestration worker; "
            "install the optional extra: uv pip install -e '.[temporal]'"
        )
```

- [ ] **Step 3: Register it in `worker.py`** — add `_make_human_input_workflow` to the import and the `workflows=[...]` list:

```python
    from plugins.temporal.workflows import _make_workflow, _make_background_workflow, _make_human_input_workflow
    ...
    workflows=[_make_workflow(), _make_background_workflow(), _make_human_input_workflow()],
```

- [ ] **Step 4: Verify imports clean + suite green**

Run: `python -c "import plugins.temporal.workflows, plugins.temporal.worker; print('ok')"` → `ok`.
Run: `scripts/run_tests.sh tests/plugins/temporal/` → all prior tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/workflows.py plugins/temporal/worker.py
git commit -m "feat(temporal): HumanInputWorkflow (signal + timeout) (Phase 3)"
```

---

## Task 3: tools — `durable_ask`, `dispatch_human_input`, `signal_human_input`, status

**Files:**
- Modify: `plugins/temporal/tools.py`
- Test: `tests/plugins/temporal/test_durable_ask.py`, `tests/plugins/temporal/test_human_input_authz.py`

**Interfaces:**
- Produces: `DURABLE_ASK_SCHEMA`; `handle_durable_ask(args, **kw) -> str`; `dispatch_human_input(*, prompt, choices, context, session_key, timeout_seconds) -> dict`; `signal_human_input(run_id: str, answer: str, session_key: str) -> dict`. Extends `handle_durable_status` to report `waiting_for_input`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/plugins/temporal/test_durable_ask.py
import json
from plugins.temporal import tools

class _FakeHandle:
    id = "durable-ask-abc"
class _FakeClient:
    async def start_workflow(self, *a, **kw):
        assert kw.get("task_queue"); return _FakeHandle()

def test_durable_ask_returns_waiting(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(tools, "load_config", lambda: {"temporal": {"enabled": True, "target": "localhost:7233", "namespace": "default", "task_queue": "hermes"}})
    async def fake_connect(s): return _FakeClient()
    monkeypatch.setattr(tools, "connect", fake_connect)
    monkeypatch.setattr("tools.approval.get_current_session_key", lambda default="default": "sessA")
    out = json.loads(tools.handle_durable_ask({"prompt": "Approve? (yes/no)", "choices": ["yes", "no"]}))
    assert out["status"] == "waiting"
    assert out["run_id"] == "durable-ask-abc"
    # waiting notice persisted for the session
    from plugins.temporal import outbox
    assert outbox.get_row("durable-ask-abc:waiting")["session_key"] == "sessA"

def test_durable_ask_requires_prompt():
    out = json.loads(tools.handle_durable_ask({}))
    assert out["status"] == "error"
    assert "prompt" in out["error"]
```

```python
# tests/plugins/temporal/test_human_input_authz.py
import json
from plugins.temporal import tools, outbox

def test_signal_rejects_session_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("run-x:waiting", "owner", "waiting", {"prompt": "?"})
    out = json.loads(json.dumps(tools.signal_human_input("run-x", "yes", session_key="intruder")))
    assert out["status"] == "error"
    assert "authoriz" in out["error"].lower() or "session" in out["error"].lower()
```

- [ ] **Step 2: Run them — expect FAIL** (`AttributeError: handle_durable_ask` / `signal_human_input`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_durable_ask.py tests/plugins/temporal/test_human_input_authz.py`

- [ ] **Step 3: Implement in `tools.py`** (append; reuse the existing `connect`, `resolve_temporal_config`, `load_config`, `uuid`, `json`, `asyncio`)

```python
from plugins.temporal import outbox as _outbox

DURABLE_ASK_SCHEMA = {
    "name": "durable_ask",
    "description": "Ask a human a question and pause durably until they respond via "
                   "`hermes temporal respond <run_id> \"<answer>\"`. Survives restart. "
                   "Returns a run_id; the answer re-enters the conversation when given.",
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "choices": {"type": "array", "items": {"type": "string"}},
            "context": {"type": "string"},
            "timeout_seconds": {"type": "integer", "description": "Default 86400 (1 day)."},
        },
        "required": ["prompt"],
    },
}


def dispatch_human_input(*, prompt, choices, context, session_key, timeout_seconds) -> dict:
    s = resolve_temporal_config(load_config())
    run_id = f"durable-ask-{uuid.uuid4().hex[:12]}"
    async def _go():
        client = await connect(s)
        await client.start_workflow(
            "HumanInputWorkflow",
            {"prompt": prompt, "choices": choices, "context": context,
             "session_key": session_key or "default", "run_id": run_id,
             "timeout_seconds": int(timeout_seconds or 86400)},
            id=run_id, task_queue=s.task_queue,
        )
        return run_id
    rid = asyncio.run(_go())
    # durable "waiting" notice so the pending question survives restart and is visible
    _outbox.record_completion(f"{rid}:waiting", session_key or "default", "waiting",
                              {"goal": prompt, "summary": f"Awaiting human input: {prompt}",
                               "prompt": prompt, "choices": choices, "status": "waiting"})
    return {"status": "waiting", "run_id": rid}


def handle_durable_ask(args: dict, **kw) -> str:
    prompt = args.get("prompt")
    if not prompt:
        return json.dumps({"status": "error", "error": "`prompt` is required"})
    s = resolve_temporal_config(load_config())
    if not s.enabled:
        return json.dumps({"status": "error",
            "error": "durable_ask requires temporal.enabled; see docs/temporal/. Not falling back."})
    from tools.approval import get_current_session_key
    try:
        out = dispatch_human_input(
            prompt=prompt, choices=args.get("choices"), context=args.get("context"),
            session_key=get_current_session_key(default="default"),
            timeout_seconds=args.get("timeout_seconds"))
    except Exception as e:  # noqa: BLE001
        return json.dumps({"status": "error", "error": f"durable_ask failed: {e}"})
    return json.dumps(out)


def signal_human_input(run_id: str, answer: str, session_key: str) -> dict:
    row = _outbox.get_row(f"{run_id}:waiting")
    if row is None:
        return {"status": "error", "error": f"no pending durable_ask for run_id {run_id}"}
    if (row.get("session_key") or "default") != (session_key or "default"):
        return {"status": "error", "error": "not authorized: respond must come from the originating session"}
    s = resolve_temporal_config(load_config())
    async def _go():
        client = await connect(s)
        handle = client.get_workflow_handle(run_id)
        await handle.signal("respond", answer)
    try:
        asyncio.run(_go())
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"signal failed: {e}"}
    return {"status": "ok", "run_id": run_id}
```

Extend `handle_durable_status`: when the workflow is still running AND a `<run_id>:waiting` outbox row exists, return `{"status": "waiting_for_input", "run_id", "prompt": row["block"].get("prompt")}`. Add at the top of `_status(run_id)` (before/after the describe call as fits the existing code):
```python
    _w = _outbox.get_row(f"{run_id}:waiting")
    # (after determining the workflow is still running:)
    #   if _w is not None: return {"status": "waiting_for_input", "run_id": run_id, "prompt": _w["block"].get("prompt")}
```
(Implement consistent with the existing `_status` structure; keep the completed/failed branches intact.)

- [ ] **Step 4: Run them — expect PASS**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_durable_ask.py tests/plugins/temporal/test_human_input_authz.py`

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/tools.py tests/plugins/temporal/test_durable_ask.py tests/plugins/temporal/test_human_input_authz.py
git commit -m "feat(temporal): durable_ask + dispatch/signal human input + waiting status (Phase 3)"
```

---

## Task 4: register `durable_ask` tool + `respond` CLI subcommand + `/respond` slash command

**Files:**
- Modify: `plugins/temporal/__init__.py`
- Modify: `plugins/temporal/worker.py`
- Test: `tests/plugins/temporal/test_respond_command.py`

**Interfaces:**
- Consumes: `handle_durable_ask`, `signal_human_input` (Task 3).
- Produces: `durable_ask` registered tool; `/respond` slash command; `hermes temporal respond` subcommand.

- [ ] **Step 1: Write the failing test** (the slash-command handler is pure-ish; mock signal)

```python
# tests/plugins/temporal/test_respond_command.py
import plugins.temporal as tp

def test_respond_command_parses_and_signals(monkeypatch):
    calls = {}
    def fake_signal(run_id, answer, session_key):
        calls.update(run_id=run_id, answer=answer, session_key=session_key)
        return {"status": "ok", "run_id": run_id}
    monkeypatch.setattr("plugins.temporal.tools.signal_human_input", fake_signal)
    monkeypatch.setattr("tools.approval.get_current_session_key", lambda default="default": "sessA")
    out = tp._respond_command('durable-ask-abc "yes please"')
    assert calls["run_id"] == "durable-ask-abc"
    assert calls["answer"] == "yes please"
    assert calls["session_key"] == "sessA"
    assert "ok" in out.lower() or "durable-ask-abc" in out

def test_respond_command_usage_when_missing_args():
    out = tp._respond_command("")
    assert "usage" in out.lower()
```

- [ ] **Step 2: Run it — expect FAIL** (`AttributeError: _respond_command`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_respond_command.py`

- [ ] **Step 3: Implement in `plugins/temporal/__init__.py`** — add the `durable_ask` tool registration, the `_respond_command` handler, register the `/respond` slash command, and extend `_setup` with the respond subparser.

Add tool registration inside `register(ctx)` (next to the existing tools):
```python
    ctx.register_tool(
        name="durable_ask", toolset="temporal",
        schema=_tools.DURABLE_ASK_SCHEMA, handler=_tools.handle_durable_ask,
        check_fn=temporal_available, description="Pause durably for human input.",
        emoji="⏸️",
    )
```

Add the slash-command handler at module level + register it:
```python
import shlex as _shlex

def _respond_command(raw_args: str) -> str:
    """/respond <run_id> "<answer>" — signal a waiting durable_ask."""
    try:
        parts = _shlex.split(raw_args or "")
    except ValueError:
        parts = (raw_args or "").split()
    if len(parts) < 2:
        return "usage: /respond <run_id> \"<answer>\""
    run_id, answer = parts[0], " ".join(parts[1:])
    from tools.approval import get_current_session_key
    from plugins.temporal import tools as _t
    res = _t.signal_human_input(run_id, answer, get_current_session_key(default="default"))
    if res.get("status") == "ok":
        return f"Responded to {run_id}."
    return f"respond error: {res.get('error')}"
```
In `register(ctx)`:
```python
    ctx.register_command(name="respond", handler=_respond_command,
                         description="Answer a waiting durable_ask",
                         args_hint="<run_id> <answer>")
```

Extend the `_setup` for the CLI command (it currently only wires the worker subcommand):
```python
    def _setup(subparser):
        sub = subparser.add_subparsers(dest="temporal_command")
        setup_worker_parser(sub)
        setup_respond_parser(sub)
```
(import `setup_respond_parser` and switch `handler_fn` to a dispatcher — see Step 4.)

- [ ] **Step 4: Add the CLI respond subparser + dispatcher to `worker.py`**

```python
def setup_respond_parser(subparsers) -> None:
    p = subparsers.add_parser("respond", help="Answer a waiting durable_ask")
    p.add_argument("run_id")
    p.add_argument("answer")

def cmd_temporal(args) -> int:
    """Dispatch the `hermes temporal <subcommand>`."""
    if getattr(args, "temporal_command", None) == "respond":
        from plugins.temporal.tools import signal_human_input
        from tools.approval import get_current_session_key
        res = signal_human_input(args.run_id, args.answer, get_current_session_key(default="default"))
        print(res.get("error") or f"Responded to {args.run_id}.")
        return 0 if res.get("status") == "ok" else 1
    return cmd_temporal_worker(args)  # default: worker
```
In `__init__.py`, import `cmd_temporal`/`setup_respond_parser` and pass `handler_fn=cmd_temporal` to `register_cli_command`.

- [ ] **Step 5: Run it — expect PASS** + suite

Run: `scripts/run_tests.sh tests/plugins/temporal/test_respond_command.py tests/plugins/temporal/`
Run: `python -c "import plugins.temporal as tp; print('durable_ask' in [s['name'] for s in []] or hasattr(tp,'_respond_command'))"` (sanity: module imports, handler exists).

- [ ] **Step 6: Commit**

```bash
git add plugins/temporal/__init__.py plugins/temporal/worker.py tests/plugins/temporal/test_respond_command.py
git commit -m "feat(temporal): register durable_ask + /respond + `hermes temporal respond` (Phase 3)"
```

---

## Task 5: gated e2e (signal + timeout) + docs/gate

**Files:**
- Create: `tests/plugins/temporal/test_phase3_integration.py`
- Modify: `AGENTS.md`

- [ ] **Step 1: Write the gated e2e**

```python
# tests/plugins/temporal/test_phase3_integration.py
import uuid, asyncio
import pytest
pytest.importorskip("temporalio")
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity
from plugins.temporal.workflows import _make_human_input_workflow
from plugins.temporal import outbox

pytestmark = pytest.mark.integration

@activity.defn(name="record_outbox")
async def real_record(payload: dict) -> None:
    outbox.record_completion(payload["run_id"], payload["session_key"], payload["status"], payload["block"])

@pytest.mark.asyncio
async def test_signal_resumes_and_delivers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"hermes-p3-{uuid.uuid4().hex[:8]}"; run_id = f"durable-ask-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=tq,
                          workflows=[_make_human_input_workflow()], activities=[real_record]):
            h = await env.client.start_workflow(
                "HumanInputWorkflow",
                {"prompt": "ok?", "session_key": "sessA", "run_id": run_id, "timeout_seconds": 3600},
                id=run_id, task_queue=tq)
            await h.signal("respond", "yes")
            res = await h.result()
    assert res["status"] == "answered"
    assert outbox.get_row(run_id)["block"]["summary"] == "yes"

@pytest.mark.asyncio
async def test_timeout_completes_timed_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"hermes-p3-{uuid.uuid4().hex[:8]}"; run_id = f"durable-ask-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=tq,
                          workflows=[_make_human_input_workflow()], activities=[real_record]):
            res = await env.client.execute_workflow(
                "HumanInputWorkflow",
                {"prompt": "ok?", "session_key": "sessA", "run_id": run_id, "timeout_seconds": 1},
                id=run_id, task_queue=tq)
    assert res["status"] == "timed_out"
    assert res["block"]["summary"] is None
```

- [ ] **Step 2: Run it** (temporalio installed)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_phase3_integration.py -- -m integration -o "addopts="`
Expected: 2 passed (signal path + timeout path). The time-skipping env fast-forwards the 1s timeout. If a real bug surfaces (as in prior phases), fix the minimal cause and note it. Without temporalio: SKIPPED.

- [ ] **Step 3: Extend AGENTS.md** Temporal section (~6-10 lines): `durable_ask(prompt, choices?, timeout_seconds?)` pauses durably for human input; respond via `hermes temporal respond <run_id> "<answer>"` or `/respond`; answer re-enters via the outbox; default 1-day timeout → `timed_out`; respond restricted to the originating session; requires `temporal.enabled`.

- [ ] **Step 4: Full gate**

Run: `scripts/run_tests.sh tests/plugins/temporal/` and `ruff check plugins/temporal/`. Record output; note the e2e SKIPs without temporalio.

- [ ] **Step 5: Commit**

```bash
git add tests/plugins/temporal/test_phase3_integration.py AGENTS.md
git commit -m "test(temporal): HITL signal+timeout e2e; docs (Phase 3)"
```

---

## Self-review notes (coverage)

- Spec `durable_ask` + `HumanInputWorkflow` (signal + timer): Task 2 (workflow) + Task 3 (tool). ✓
- Spec response channel (`respond` CLI + `/respond` gateway): Task 4. ✓
- Spec answer re-enters via Phase 2 outbox rail: Task 2 calls `record_outbox` (Phase 2 activity); delivery is the existing Phase 2 drain (no new work). ✓
- Spec durable "waiting" notice (survives restart, distinct row): Task 3 `dispatch_human_input` writes `<run_id>:waiting`. ✓
- Spec timeout default 86400 → `timed_out`, no auto-default: Task 2 workflow. ✓
- Spec authorization (session-restricted, from `<run_id>:waiting` row): Task 1 `get_row` + Task 3 `signal_human_input`. ✓
- Spec `durable_status` waiting_for_input: Task 3. ✓
- Spec gating / no silent fallback: Task 3 `handle_durable_ask`. ✓
- Spec existing `/approve` untouched: nothing in the plan modifies `tools/approval.py`. ✓
- Out of scope (P4): absent. ✓
- Name consistency: `_make_human_input_workflow` (Task 2, used Task 2 worker + Task 5 e2e); `dispatch_human_input`/`signal_human_input`/`handle_durable_ask`/`DURABLE_ASK_SCHEMA` (Task 3, used Task 4); `outbox.get_row` (Task 1, used Task 3); `cmd_temporal`/`setup_respond_parser`/`_respond_command` (Task 4). ✓
