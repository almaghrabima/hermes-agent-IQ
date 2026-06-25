# Temporal Durable Orchestration (Phase 0 + 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Design:** `docs/temporal/2026-06-25-temporal-phase0-1-design.md`

**Goal:** Add an opt-in `plugins/temporal/` plugin that lets agents run reliable, retrying multi-step jobs as Temporal workflows via two service-gated tools (`durable_run`, `durable_status`), with a separate worker process and support for both Temporal Cloud and a local dev-server (default).

**Architecture:** The agent process holds only a lightweight Temporal **client** (start/query workflows). A separate `hermes temporal worker` process hosts `DurableRunWorkflow` and `run_step_activity`. Steps execute as Temporal activities with a RetryPolicy; each step runs a subagent via the existing `delegate_task` handler. The synchronous agent loop and prompt-caching are untouched; the tools are `check_fn`-gated so they never appear unless Temporal is enabled and reachable.

**Tech Stack:** Python 3.11 (`temporalio` SDK), Hermes plugin system (`plugins/<name>/` + `PluginContext`), `tools/registry.py`, `tools/lazy_deps.py`, `temporal server start-dev` for local/dev.

## Global Constraints

- **Prompt caching is sacred** — do NOT modify `run_agent.py`'s loop, message ordering, or system prompt. Durable work is started only via the explicit tools.
- **Narrow waist** — `durable_run`/`durable_status` are plugin tools registered through `PluginContext.register_tool(...)`, gated by `check_fn`. Never add to `_HERMES_CORE_TOOLS`.
- **Dependency policy** — `temporalio` is an exact-pinned **optional extra** (`[temporal]`), NOT in `[all]`, and lazy-installed via `tools/lazy_deps.py` under key `tool.temporal`. Pin the concrete latest-stable version at Task 1 (`temporalio==X.Y.Z`).
- **Config vs secrets** — behavioral settings under `temporal:` in `config.yaml`; secrets (`TEMPORAL_API_KEY`, mTLS cert/key paths) only in `.env`. No new non-secret `HERMES_*` env vars.
- **Profile-aware paths** — resolve via `get_hermes_home()` / `load_config()` / `cfg_get`; never hardcode `~/.hermes`.
- **Tests** — run via `scripts/run_tests.sh`; tests use the autouse `_hermetic_environment` fixture (temp `HERMES_HOME`). Integration tests that need a real Temporal server are **gated and skip** when the `temporal` binary is absent (mirrors the rlm docker/KVM e2e pattern). No change-detector tests.
- **Plugin opt-in** — `kind: standalone`; activates only when listed in `plugins.enabled` AND `temporal.enabled: true`.

## File Structure

- Create: `plugins/temporal/plugin.yaml` — manifest (`kind: standalone`, `provides_tools`).
- Create: `plugins/temporal/__init__.py` — `register(ctx)`: config-gated tool + CLI registration.
- Create: `plugins/temporal/tconfig.py` — `resolve_temporal_config()` → typed settings (dev/Cloud/disabled).
- Create: `plugins/temporal/client.py` — `connect()` async client factory (lazy-imports `temporalio`).
- Create: `plugins/temporal/workflows.py` — `DurableRunWorkflow`.
- Create: `plugins/temporal/activities.py` — `run_step_activity` + `execute_durable_step`.
- Create: `plugins/temporal/tools.py` — `durable_run` / `durable_status` handlers + JSON schemas.
- Create: `plugins/temporal/worker.py` — worker bootstrap + `setup_worker_parser` / `cmd_temporal_worker`; dev-server auto-start helper.
- Modify: `pyproject.toml` — add `[project.optional-dependencies] temporal`.
- Modify: `tools/lazy_deps.py` — add `tool.temporal` entry.
- Create tests under `tests/plugins/temporal/`:
  `test_tconfig.py`, `test_gating.py`, `test_tools.py`, `test_client.py`, `test_integration.py` (gated).

---

## Task 1: Packaging — `[temporal]` extra + lazy-dep entry

**Files:**
- Modify: `pyproject.toml` (`[project.optional-dependencies]`)
- Modify: `tools/lazy_deps.py` (`LAZY_DEPS`)
- Test: `tests/plugins/temporal/test_packaging.py`

**Interfaces:**
- Produces: lazy-dep key `"tool.temporal"` → `("temporalio==X.Y.Z",)`; extra `temporal`.

- [ ] **Step 1: Pin the version.** Run `uv pip index versions temporalio` (or check PyPI) and record the latest stable as `X.Y.Z`. Use that exact value in all steps below.

- [ ] **Step 2: Write the failing test**

```python
# tests/plugins/temporal/test_packaging.py
from tools.lazy_deps import LAZY_DEPS

def test_temporal_lazy_dep_registered():
    assert "tool.temporal" in LAZY_DEPS
    pkgs = LAZY_DEPS["tool.temporal"]
    assert any(p.startswith("temporalio==") for p in pkgs), pkgs
```

- [ ] **Step 3: Run it — expect FAIL**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_packaging.py`
Expected: FAIL — `KeyError`/assert (`tool.temporal` not in `LAZY_DEPS`).

- [ ] **Step 4: Add the lazy-dep entry** to `tools/lazy_deps.py` inside the `LAZY_DEPS` dict:

```python
    "tool.temporal": (
        "temporalio==X.Y.Z",
    ),
```

- [ ] **Step 5: Add the optional extra** to `pyproject.toml` under `[project.optional-dependencies]`:

```toml
temporal = ["temporalio==X.Y.Z"]  # durable orchestration plugin; NOT in [all] (lazy-installed)
```

- [ ] **Step 6: Run it — expect PASS**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_packaging.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml tools/lazy_deps.py tests/plugins/temporal/test_packaging.py
git commit -m "feat(temporal): add [temporal] extra + lazy-dep entry (Phase 0)"
```

---

## Task 2: Config resolution — `tconfig.py`

**Files:**
- Create: `plugins/temporal/tconfig.py`
- Test: `tests/plugins/temporal/test_tconfig.py`

**Interfaces:**
- Produces: `@dataclass TemporalSettings(enabled: bool, target: str, namespace: str, tls: bool, task_queue: str, dev_server: bool, step_timeout_seconds: int, retry: dict, api_key: str | None, tls_cert: str | None, tls_key: str | None)` and `resolve_temporal_config(config: dict | None = None, env: dict | None = None) -> TemporalSettings`.

- [ ] **Step 1: Write the failing test**

```python
# tests/plugins/temporal/test_tconfig.py
from plugins.temporal.tconfig import resolve_temporal_config

def test_defaults_to_dev_server_disabled():
    s = resolve_temporal_config(config={}, env={})
    assert s.enabled is False
    assert s.target == "localhost:7233"
    assert s.namespace == "default"
    assert s.tls is False
    assert s.dev_server is True
    assert s.task_queue == "hermes"

def test_cloud_config_with_api_key_from_env():
    cfg = {"temporal": {"enabled": True, "target": "ns.acct.tmprl.cloud:7233",
                         "namespace": "ns.acct", "tls": True}}
    s = resolve_temporal_config(config=cfg, env={"TEMPORAL_API_KEY": "sek"})
    assert s.enabled is True
    assert s.tls is True
    assert s.api_key == "sek"
    assert s.target.endswith(":7233")

def test_retry_and_timeout_defaults_overridable():
    cfg = {"temporal": {"step_timeout_seconds": 120,
                        "default_retry": {"max_attempts": 5}}}
    s = resolve_temporal_config(config=cfg, env={})
    assert s.step_timeout_seconds == 120
    assert s.retry["max_attempts"] == 5
    assert s.retry["backoff_coefficient"] == 2.0  # untouched default
```

- [ ] **Step 2: Run it — expect FAIL** (`ModuleNotFoundError: plugins.temporal.tconfig`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_tconfig.py`

- [ ] **Step 3: Implement `tconfig.py`**

```python
# plugins/temporal/tconfig.py
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Optional

_DEFAULT_RETRY = {"max_attempts": 3, "initial_interval_seconds": 1, "backoff_coefficient": 2.0}


@dataclass
class TemporalSettings:
    enabled: bool = False
    target: str = "localhost:7233"
    namespace: str = "default"
    tls: bool = False
    task_queue: str = "hermes"
    dev_server: bool = True
    step_timeout_seconds: int = 600
    retry: dict = field(default_factory=lambda: dict(_DEFAULT_RETRY))
    api_key: Optional[str] = None
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None


def resolve_temporal_config(config: Optional[dict] = None, env: Optional[dict] = None) -> TemporalSettings:
    """Resolve the ``temporal:`` block from config.yaml + secrets from env."""
    config = config or {}
    env = env if env is not None else os.environ
    t = config.get("temporal") or {}
    retry = dict(_DEFAULT_RETRY)
    retry.update(t.get("default_retry") or {})
    return TemporalSettings(
        enabled=bool(t.get("enabled", False)),
        target=str(t.get("target", "localhost:7233")),
        namespace=str(t.get("namespace", "default")),
        tls=bool(t.get("tls", False)),
        task_queue=str(t.get("task_queue", "hermes")),
        dev_server=bool(t.get("dev_server", True)),
        step_timeout_seconds=int(t.get("step_timeout_seconds", 600)),
        retry=retry,
        api_key=env.get("TEMPORAL_API_KEY"),
        tls_cert=env.get("TEMPORAL_TLS_CERT"),
        tls_key=env.get("TEMPORAL_TLS_KEY"),
    )
```

- [ ] **Step 4: Run it — expect PASS**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_tconfig.py`

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/tconfig.py tests/plugins/temporal/test_tconfig.py
git commit -m "feat(temporal): config resolution (dev/Cloud/disabled) (Phase 0)"
```

---

## Task 3: Client factory — `client.py`

**Files:**
- Create: `plugins/temporal/client.py`
- Test: `tests/plugins/temporal/test_client.py`

**Interfaces:**
- Consumes: `TemporalSettings` (Task 2).
- Produces: `build_connect_kwargs(s: TemporalSettings) -> dict` and `async def connect(s: TemporalSettings)`.

- [ ] **Step 1: Write the failing test** (test the pure kwargs builder — no network)

```python
# tests/plugins/temporal/test_client.py
from plugins.temporal.tconfig import TemporalSettings
from plugins.temporal.client import build_connect_kwargs

def test_dev_kwargs_minimal():
    kw = build_connect_kwargs(TemporalSettings(target="localhost:7233", namespace="default"))
    assert kw["target_host"] == "localhost:7233"
    assert kw["namespace"] == "default"
    assert "api_key" not in kw or kw["api_key"] is None

def test_cloud_kwargs_include_api_key_and_tls():
    s = TemporalSettings(target="ns.acct.tmprl.cloud:7233", namespace="ns.acct",
                         tls=True, api_key="sek")
    kw = build_connect_kwargs(s)
    assert kw["tls"] is True
    assert kw["api_key"] == "sek"
```

- [ ] **Step 2: Run it — expect FAIL** (`ModuleNotFoundError`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_client.py`

- [ ] **Step 3: Implement `client.py`**

```python
# plugins/temporal/client.py
from __future__ import annotations
from typing import Any
from plugins.temporal.tconfig import TemporalSettings


def build_connect_kwargs(s: TemporalSettings) -> dict[str, Any]:
    """Assemble temporalio Client.connect kwargs from settings (pure; no I/O)."""
    kw: dict[str, Any] = {"target_host": s.target, "namespace": s.namespace}
    if s.tls:
        kw["tls"] = True
    if s.api_key:
        kw["api_key"] = s.api_key
    return kw


async def connect(s: TemporalSettings):
    """Connect a Temporal client. Lazy-imports temporalio (raises FeatureUnavailable)."""
    from tools.lazy_deps import ensure
    ensure("tool.temporal", prompt=False)
    from temporalio.client import Client  # type: ignore
    return await Client.connect(**build_connect_kwargs(s))
```

- [ ] **Step 4: Run it — expect PASS**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_client.py`

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/client.py tests/plugins/temporal/test_client.py
git commit -m "feat(temporal): client connect-kwargs builder + connect() (Phase 0)"
```

---

## Task 4: Activity + step executor — `activities.py`

**Files:**
- Create: `plugins/temporal/activities.py`
- Test: `tests/plugins/temporal/test_activities.py`

**Interfaces:**
- Produces: `execute_durable_step(step: dict) -> dict` (pure-ish; runs one subagent step) and the Temporal-decorated `run_step_activity(step: dict) -> dict` wrapping it.

- [ ] **Step 1: Verify the delegate handler seam.** Run:
`grep -n "register(" tools/delegate_tool.py | head` and
`grep -n "handler=" tools/delegate_tool.py | head`
Confirm the registered tool name is `delegate_task` and note its `handler=` callable. Confirm `ToolEntry` exposes `.handler`:
`grep -n "class ToolEntry\|handler" tools/registry.py | head`. The handler is reachable as `registry._tools["delegate_task"].handler`.

- [ ] **Step 2: Write the failing test** (inject a fake step runner so no real agent runs)

```python
# tests/plugins/temporal/test_activities.py
from plugins.temporal import activities

def test_execute_durable_step_calls_runner(monkeypatch):
    captured = {}
    def fake_runner(args, **kw):
        captured.update(args)
        return '{"status": "success", "result": "done"}'
    monkeypatch.setattr(activities, "_delegate_handler", lambda: fake_runner)
    out = activities.execute_durable_step({"name": "s1", "prompt": "do x", "sub_agent": "m"})
    assert captured["goal"] == "do x"
    assert out["name"] == "s1"
    assert out["ok"] is True
    assert "done" in out["result"]
```

- [ ] **Step 3: Run it — expect FAIL** (`ModuleNotFoundError`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_activities.py`

- [ ] **Step 4: Implement `activities.py`**

```python
# plugins/temporal/activities.py
from __future__ import annotations
import json
from typing import Any, Callable


def _delegate_handler() -> Callable:
    """Return the registered delegate_task handler (subagent runner)."""
    from tools.registry import registry
    return registry._tools["delegate_task"].handler


def execute_durable_step(step: dict) -> dict:
    """Run one durable step as a single subagent delegation. Pure of Temporal."""
    handler = _delegate_handler()
    raw = handler({"goal": step["prompt"], "sub_agent": step.get("sub_agent")})
    text = raw if isinstance(raw, str) else json.dumps(raw)
    try:
        parsed = json.loads(text)
        ok = parsed.get("status") == "success"
        result = parsed.get("result", text)
    except Exception:
        ok, result = True, text
    return {"name": step.get("name", ""), "ok": ok, "result": result}


# Temporal activity wrapper — imported lazily so non-temporal runs never import temporalio.
def _make_activity():
    from temporalio import activity  # type: ignore

    @activity.defn(name="run_step")
    async def run_step_activity(step: dict) -> dict:
        return execute_durable_step(step)

    return run_step_activity
```

- [ ] **Step 5: Run it — expect PASS**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_activities.py`

- [ ] **Step 6: Commit**

```bash
git add plugins/temporal/activities.py tests/plugins/temporal/test_activities.py
git commit -m "feat(temporal): durable step executor + activity wrapper (Phase 1)"
```

---

## Task 5: Workflow — `workflows.py`

**Files:**
- Create: `plugins/temporal/workflows.py`
- Test: covered by the gated integration test (Task 8); workflow code can't run without temporalio.

**Interfaces:**
- Produces: `DurableRunWorkflow` with `@workflow.run async def run(self, params: dict) -> dict` executing each step via `run_step` activity with `RetryPolicy`.

- [ ] **Step 1: Implement `workflows.py`** (no isolated unit test — exercised in Task 8 under the time-skipping test server)

```python
# plugins/temporal/workflows.py
from __future__ import annotations
from datetime import timedelta


def _make_workflow():
    from temporalio import workflow  # type: ignore
    from temporalio.common import RetryPolicy  # type: ignore

    @workflow.defn(name="DurableRunWorkflow")
    class DurableRunWorkflow:
        @workflow.run
        async def run(self, params: dict) -> dict:
            steps = params.get("steps", [])
            retry = params.get("retry") or {}
            timeout_s = int(params.get("step_timeout_seconds", 600))
            policy = RetryPolicy(
                maximum_attempts=int(retry.get("max_attempts", 3)),
                initial_interval=timedelta(seconds=int(retry.get("initial_interval_seconds", 1))),
                backoff_coefficient=float(retry.get("backoff_coefficient", 2.0)),
            )
            results = []
            for step in steps:
                r = await workflow.execute_activity(
                    "run_step", step,
                    start_to_close_timeout=timedelta(seconds=timeout_s),
                    retry_policy=policy,
                )
                results.append(r)
            return {"steps": results, "completed": len(results)}

    return DurableRunWorkflow
```

- [ ] **Step 2: Commit**

```bash
git add plugins/temporal/workflows.py
git commit -m "feat(temporal): DurableRunWorkflow orchestrating retrying steps (Phase 1)"
```

---

## Task 6: Worker — `worker.py`

**Files:**
- Create: `plugins/temporal/worker.py`
- Test: `tests/plugins/temporal/test_worker.py` (arg/parser wiring only; running the worker is Task 8)

**Interfaces:**
- Consumes: `connect` (Task 3), `_make_workflow` (Task 5), `_make_activity` (Task 4), `resolve_temporal_config` (Task 2).
- Produces: `async def run_worker(s)`, `setup_worker_parser(subparser)`, `cmd_temporal_worker(args)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/plugins/temporal/test_worker.py
import argparse
from plugins.temporal import worker

def test_setup_worker_parser_adds_worker_subcommand():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="temporal_command")
    worker.setup_worker_parser(sub)
    ns = p.parse_args(["worker"])
    assert ns.temporal_command == "worker"
```

- [ ] **Step 2: Run it — expect FAIL** (`ModuleNotFoundError`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_worker.py`

- [ ] **Step 3: Implement `worker.py`**

```python
# plugins/temporal/worker.py
from __future__ import annotations
import asyncio
from hermes_cli.config import load_config
from plugins.temporal.tconfig import resolve_temporal_config
from plugins.temporal.client import connect


async def run_worker(s) -> None:
    from temporalio.worker import Worker  # type: ignore
    from plugins.temporal.workflows import _make_workflow
    from plugins.temporal.activities import _make_activity
    client = await connect(s)
    worker = Worker(
        client,
        task_queue=s.task_queue,
        workflows=[_make_workflow()],
        activities=[_make_activity()],
    )
    await worker.run()


def setup_worker_parser(subparsers) -> None:
    """Attach `hermes temporal worker` (called by register_cli_command setup_fn)."""
    subparsers.add_parser("worker", help="Run the Temporal worker for the hermes task queue")


def cmd_temporal_worker(args) -> int:
    s = resolve_temporal_config(load_config())
    if not s.enabled:
        print("temporal.enabled is false in config.yaml — nothing to run.")
        return 1
    asyncio.run(run_worker(s))
    return 0
```

- [ ] **Step 4: Run it — expect PASS**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_worker.py`

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/worker.py tests/plugins/temporal/test_worker.py
git commit -m "feat(temporal): worker bootstrap + `hermes temporal worker` (Phase 0)"
```

---

## Task 7: Tools — `tools.py` (`durable_run` / `durable_status`)

**Files:**
- Create: `plugins/temporal/tools.py`
- Test: `tests/plugins/temporal/test_tools.py`

**Interfaces:**
- Consumes: `resolve_temporal_config`, `connect` (mocked in tests).
- Produces: `DURABLE_RUN_SCHEMA`, `DURABLE_STATUS_SCHEMA`, `handle_durable_run(args, **kw) -> str`, `handle_durable_status(args, **kw) -> str`.

- [ ] **Step 1: Write the failing test** (mock the client so no server is needed)

```python
# tests/plugins/temporal/test_tools.py
import json
from plugins.temporal import tools

class _FakeHandle:
    id = "run-123"
    async def result(self):
        return {"steps": [{"name": "s1", "ok": True, "result": "done"}], "completed": 1}

class _FakeClient:
    async def start_workflow(self, *a, **kw):
        return _FakeHandle()

def test_durable_run_returns_completed(monkeypatch):
    async def fake_connect(s):
        return _FakeClient()
    monkeypatch.setattr(tools, "connect", fake_connect)
    out = json.loads(tools.handle_durable_run(
        {"steps": [{"name": "s1", "prompt": "do x"}], "wait_seconds": 5}))
    assert out["status"] == "completed"
    assert out["run_id"] == "run-123"
    assert out["result"]["completed"] == 1

def test_durable_run_arg_validation():
    out = json.loads(tools.handle_durable_run({"steps": []}))
    assert out["status"] == "error"
    assert "steps" in out["error"]
```

- [ ] **Step 2: Run it — expect FAIL** (`ModuleNotFoundError`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_tools.py`

- [ ] **Step 3: Implement `tools.py`**

```python
# plugins/temporal/tools.py
from __future__ import annotations
import asyncio
import json
import uuid
from hermes_cli.config import load_config
from plugins.temporal.tconfig import resolve_temporal_config
from plugins.temporal.client import connect

DURABLE_RUN_SCHEMA = {
    "name": "durable_run",
    "description": "Run an ordered list of steps as a durable, retrying Temporal workflow. "
                   "Each step is a subagent task. Returns a run_id; long runs are polled with durable_status.",
    "parameters": {
        "type": "object",
        "properties": {
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "prompt": {"type": "string"},
                "sub_agent": {"type": "string"}}, "required": ["prompt"]}},
            "retry": {"type": "object"},
            "step_timeout_seconds": {"type": "integer"},
            "wait_seconds": {"type": "integer", "description": "Block up to N seconds for an inline result (default 30)."},
        },
        "required": ["steps"],
    },
}

DURABLE_STATUS_SCHEMA = {
    "name": "durable_status",
    "description": "Query a durable_run workflow by run_id; returns status and result when complete.",
    "parameters": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
}


def _err(msg: str) -> str:
    return json.dumps({"status": "error", "error": msg})


async def _run(args: dict) -> dict:
    s = resolve_temporal_config(load_config())
    client = await connect(s)
    run_id = f"durable-{uuid.uuid4().hex[:12]}"
    handle = await client.start_workflow(
        "DurableRunWorkflow",
        {"steps": args["steps"], "retry": args.get("retry"),
         "step_timeout_seconds": args.get("step_timeout_seconds", s.step_timeout_seconds)},
        id=run_id, task_queue=s.task_queue,
    )
    wait = int(args.get("wait_seconds", 30))
    try:
        result = await asyncio.wait_for(handle.result(), timeout=wait)
        return {"status": "completed", "run_id": handle.id, "result": result}
    except asyncio.TimeoutError:
        return {"status": "running", "run_id": handle.id}


def handle_durable_run(args: dict, **kw) -> str:
    steps = args.get("steps") or []
    if not steps:
        return _err("`steps` must be a non-empty array")
    if any("prompt" not in (st or {}) for st in steps):
        return _err("each step requires a `prompt`")
    try:
        return json.dumps(asyncio.run(_run(args)))
    except Exception as e:  # noqa: BLE001 — surface to the agent
        return _err(f"durable_run failed: {e}")


async def _status(run_id: str) -> dict:
    s = resolve_temporal_config(load_config())
    client = await connect(s)
    handle = client.get_workflow_handle(run_id)
    desc = await handle.describe()
    status_name = getattr(desc.status, "name", str(desc.status)).lower()
    if status_name == "completed":
        return {"status": "completed", "run_id": run_id, "result": await handle.result()}
    if status_name in ("failed", "terminated", "canceled", "timed_out"):
        return {"status": "failed", "run_id": run_id, "error": status_name}
    return {"status": "running", "run_id": run_id}


def handle_durable_status(args: dict, **kw) -> str:
    run_id = args.get("run_id")
    if not run_id:
        return _err("`run_id` is required")
    try:
        return json.dumps(asyncio.run(_status(run_id)))
    except Exception as e:  # noqa: BLE001
        return _err(f"durable_status failed: {e}")
```

- [ ] **Step 4: Run it — expect PASS**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_tools.py`

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/tools.py tests/plugins/temporal/test_tools.py
git commit -m "feat(temporal): durable_run/durable_status tool handlers (Phase 1)"
```

---

## Task 8: Plugin entry + gating — `plugin.yaml` + `__init__.py`

**Files:**
- Create: `plugins/temporal/plugin.yaml`
- Create: `plugins/temporal/__init__.py`
- Test: `tests/plugins/temporal/test_gating.py`

**Interfaces:**
- Consumes: tool schemas/handlers (Task 7), worker CLI (Task 6).
- Produces: `register(ctx)`, `temporal_available() -> bool` (check_fn).

- [ ] **Step 1: Write the failing test**

```python
# tests/plugins/temporal/test_gating.py
from plugins.temporal import temporal_available

def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr("plugins.temporal.load_config", lambda: {})
    assert temporal_available() is False

def test_available_when_enabled(monkeypatch):
    monkeypatch.setattr("plugins.temporal.load_config",
                        lambda: {"temporal": {"enabled": True, "target": "localhost:7233"}})
    # SDK import is gated separately; here we only require enabled+target.
    assert temporal_available() is True
```

- [ ] **Step 2: Run it — expect FAIL** (`ImportError: cannot import name 'temporal_available'`)

Run: `scripts/run_tests.sh tests/plugins/temporal/test_gating.py`

- [ ] **Step 3: Create `plugin.yaml`**

```yaml
# plugins/temporal/plugin.yaml
name: temporal
version: 1.0.0
description: "Durable multi-step orchestration via Temporal (durable_run / durable_status)."
author: "hermes-agent-IQ"
kind: standalone
provides_tools:
  - durable_run
  - durable_status
```

- [ ] **Step 4: Implement `__init__.py`**

```python
# plugins/temporal/__init__.py
from __future__ import annotations
from hermes_cli.config import load_config
from plugins.temporal.tconfig import resolve_temporal_config
from plugins.temporal import tools as _tools
from plugins.temporal.worker import setup_worker_parser, cmd_temporal_worker


def temporal_available() -> bool:
    """check_fn: tools appear only when temporal is enabled and a target is set."""
    try:
        s = resolve_temporal_config(load_config())
        return bool(s.enabled and s.target)
    except Exception:
        return False


def register(ctx) -> None:
    ctx.register_tool(
        name="durable_run", toolset="temporal",
        schema=_tools.DURABLE_RUN_SCHEMA, handler=_tools.handle_durable_run,
        check_fn=temporal_available, description="Run a durable, retrying multi-step job.",
        emoji="⏱️",
    )
    ctx.register_tool(
        name="durable_status", toolset="temporal",
        schema=_tools.DURABLE_STATUS_SCHEMA, handler=_tools.handle_durable_status,
        check_fn=temporal_available, description="Check a durable_run by run_id.",
        emoji="⏱️",
    )

    def _setup(subparser):
        sub = subparser.add_subparsers(dest="temporal_command")
        setup_worker_parser(sub)

    ctx.register_cli_command(
        name="temporal", help="Temporal worker / durable orchestration",
        setup_fn=_setup, handler_fn=cmd_temporal_worker,
        description="Run `hermes temporal worker` to execute durable workflows.",
    )
```

- [ ] **Step 5: Run it — expect PASS**

Run: `scripts/run_tests.sh tests/plugins/temporal/test_gating.py`

- [ ] **Step 6: Manual gating check**

Run: `python -c "from plugins.temporal import register, temporal_available; print('ok', temporal_available())"`
Expected: `ok False` (disabled by default).

- [ ] **Step 7: Commit**

```bash
git add plugins/temporal/plugin.yaml plugins/temporal/__init__.py tests/plugins/temporal/test_gating.py
git commit -m "feat(temporal): plugin entry + check_fn gating + CLI command (Phase 0/1)"
```

---

## Task 9: Gated integration e2e (real dev-server)

**Files:**
- Create: `tests/plugins/temporal/test_integration.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the gated integration test** (skips without `temporalio` + the test server)

```python
# tests/plugins/temporal/test_integration.py
import uuid
import pytest

pytest.importorskip("temporalio")
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402
from plugins.temporal.workflows import _make_workflow  # noqa: E402
from temporalio import activity  # noqa: E402

pytestmark = pytest.mark.integration

_attempts = {"n": 0}

@activity.defn(name="run_step")
async def flaky_run_step(step: dict) -> dict:
    # fail twice, then succeed — proves RetryPolicy drives it to completion
    _attempts["n"] += 1
    if _attempts["n"] < 3:
        raise RuntimeError("transient")
    return {"name": step.get("name", ""), "ok": True, "result": "done"}

@pytest.mark.asyncio
async def test_workflow_retries_then_completes():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"hermes-test-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=tq,
                          workflows=[_make_workflow()], activities=[flaky_run_step]):
            result = await env.client.execute_workflow(
                "DurableRunWorkflow",
                {"steps": [{"name": "s1", "prompt": "x"}],
                 "retry": {"max_attempts": 5, "initial_interval_seconds": 1}},
                id=f"it-{uuid.uuid4().hex[:8]}", task_queue=tq)
    assert result["completed"] == 1
    assert result["steps"][0]["ok"] is True
    assert _attempts["n"] == 3  # exactly-once after retries
```

- [ ] **Step 2: Run it**

Run: `scripts/run_tests.sh -m integration tests/plugins/temporal/test_integration.py`
Expected (with `temporalio` installed): PASS — the workflow retries the activity to success.
Expected (without `temporalio`): SKIPPED via `importorskip`. Note explicitly in the PR which case ran.

- [ ] **Step 3: Commit**

```bash
git add tests/plugins/temporal/test_integration.py
git commit -m "test(temporal): gated retry-to-completion e2e (time-skipping env) (Phase 1)"
```

---

## Task 10: Docs — config + AGENTS.md

**Files:**
- Modify: `AGENTS.md` (add a short "Temporal (`durable_run`)" subsection near the Delegation/Cron sections).

- [ ] **Step 1: Document the `temporal:` config block** in `AGENTS.md` with the exact keys from Task 2's defaults, the `.env` secrets (`TEMPORAL_API_KEY`/mTLS), the two tools, the `hermes temporal worker` command, and the dev-server-default note. Keep it concise; point to `docs/temporal/` for detail.

- [ ] **Step 2: Run the full gate**

Run:
`scripts/run_tests.sh tests/plugins/temporal/`
`ruff check plugins/temporal/`
`ty check plugins/temporal/`
Record output. The integration test SKIPs unless `temporalio` is installed — state that explicitly.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs(temporal): document durable_run/worker/config in AGENTS.md (Phase 0/1)"
```

---

## Self-review notes (coverage)

- Spec "Phase 0 rails": Tasks 1 (extra/lazy dep), 2 (config), 3 (client), 6 (worker), 8 (plugin/gating/CLI). ✓
- Spec "Phase 1 orchestration": Tasks 4 (activity/step), 5 (workflow), 7 (tools). ✓
- Spec "both deployment targets, dev-server default": Task 2 defaults + Task 3 tls/api_key kwargs. ✓
- Spec "check_fn gating": Task 8. ✓
- Spec "retries/timeouts/exactly-once": Task 5 RetryPolicy + Task 9 e2e asserting `_attempts == 3`. ✓
- Spec "testing (unit + gated integration)": Tasks 2/3/4/6/7/8 unit; Task 9 gated e2e. ✓
- Out of scope (P2 restart-resume, P3 HITL, P4 cron/kanban): not present. ✓
- One verified seam: `registry._tools["delegate_task"].handler` (Task 4 Step 1 verifies before use).
