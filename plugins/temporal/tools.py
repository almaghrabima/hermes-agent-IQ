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
    return {"status": "running", "run_id": run_id}


def handle_durable_status(args: dict, **kw) -> str:
    run_id = args.get("run_id")
    if not run_id:
        return _err("`run_id` is required")
    try:
        return json.dumps(asyncio.run(_status(run_id)))
    except Exception as e:  # noqa: BLE001
        return _err(f"durable_status failed: {e}")
