# plugins/temporal/tools.py
from __future__ import annotations
import asyncio
import json
import uuid
from hermes_cli.config import load_config
from plugins.temporal.tconfig import resolve_temporal_config
from plugins.temporal.client import connect
from plugins.temporal import outbox as _outbox  # human-input authz/waiting + reconcile skip-list

DURABLE_RUN_SCHEMA = {
    "name": "durable_run",
    "description": "Run an ordered list of steps as a durable, retrying Temporal workflow. "
                   "Each step is a subagent task. Returns a run_id; long runs are polled with durable_status.",
    "parameters": {
        "type": "object",
        "properties": {
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "prompt": {"type": "string"}},
                "required": ["prompt"]}},
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
    # Still running — check whether we're paused waiting for human input
    _w = _outbox.get_row(f"{run_id}:waiting")
    if _w is not None:
        return {"status": "waiting_for_input", "run_id": run_id, "prompt": _w["block"].get("prompt")}
    return {"status": "running", "run_id": run_id}


def handle_durable_status(args: dict, **kw) -> str:
    run_id = args.get("run_id")
    if not run_id:
        return _err("`run_id` is required")
    try:
        return json.dumps(asyncio.run(_status(run_id)))
    except Exception as e:  # noqa: BLE001
        return _err(f"durable_status failed: {e}")


def dispatch_durable_delegation(
    *,
    goal,
    context,
    toolsets,
    role,
    model,
    session_key,
    retry=None,
) -> dict:
    """Start a BackgroundDelegationWorkflow and return immediately with a run_id."""
    s = resolve_temporal_config(load_config())
    run_id = f"durable-deleg-{uuid.uuid4().hex[:12]}"

    async def _go():
        client = await connect(s)
        handle = await client.start_workflow(
            "BackgroundDelegationWorkflow",
            {
                "goal": goal,
                "context": context,
                "toolsets": toolsets,
                "role": role,
                "model": model,
                "session_key": session_key or "default",
                "run_id": run_id,
                "retry": retry,
                "step_timeout_seconds": s.step_timeout_seconds,
            },
            id=run_id,
            task_queue=s.task_queue,
        )
        return handle.id

    rid = asyncio.run(_go())
    return {"status": "dispatched", "run_id": rid}


def list_completed_durable_delegations() -> list[dict]:
    """Query Temporal for completed BackgroundDelegationWorkflows missing from the
    outbox (for reconcile). Returns [{run_id, session_key, status, block}] for the
    NOT-yet-recorded ones only — workflow id == run_id, so rows already in the
    outbox are skipped before fetching their result, bounding the per-startup scan
    to new work rather than the whole Temporal retention window. Best-effort;
    raises if temporal is down."""

    async def _go():
        s = resolve_temporal_config(load_config())
        client = await connect(s)
        out = []
        query = 'WorkflowType="BackgroundDelegationWorkflow" AND ExecutionStatus="Completed"'
        async for wf in client.list_workflows(query=query):
            if _outbox.has_run(wf.id):
                continue  # already recorded — skip the result() round-trip
            handle = client.get_workflow_handle(wf.id)
            res = await handle.result()
            out.append(
                {
                    "run_id": res.get("run_id", wf.id),
                    "session_key": res.get("session_key", "default"),
                    "status": res.get("status", "completed"),
                    "block": res.get("block", {}),
                }
            )
        return out

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# Phase 3 — human-in-the-loop: durable_ask
# ---------------------------------------------------------------------------

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
        handle = await client.start_workflow(
            "HumanInputWorkflow",
            {"prompt": prompt, "choices": choices, "context": context,
             "session_key": session_key or "default", "run_id": run_id,
             "timeout_seconds": int(timeout_seconds or 86400)},
            id=run_id, task_queue=s.task_queue,
        )
        return handle.id

    rid = asyncio.run(_go())
    # durable "waiting" notice so the pending question survives restart and is visible
    _outbox.record_completion(
        f"{rid}:waiting", session_key or "default", "waiting",
        {"goal": prompt, "summary": f"Awaiting human input: {prompt}",
         "prompt": prompt, "choices": choices, "status": "waiting"},
    )
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
            timeout_seconds=args.get("timeout_seconds"),
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps({"status": "error", "error": f"durable_ask failed: {e}"})
    return json.dumps(out)


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


def signal_human_input(run_id: str, answer: str, session_key: str, *, trusted: bool = False) -> dict:
    row = _outbox.get_row(f"{run_id}:waiting")
    if row is None:
        return {"status": "error", "error": f"no pending durable_ask for run_id {run_id}"}
    if not trusted and (row.get("session_key") or "default") != (session_key or "default"):
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
