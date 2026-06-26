# plugins/temporal/activities.py
from __future__ import annotations
import json
from typing import Callable


def _delegate_handler() -> Callable:
    """Return the registered delegate_task handler (subagent runner)."""
    from tools.registry import registry
    entry = registry._tools.get("delegate_task")
    if entry is None:
        raise RuntimeError(
            "delegate_task tool not registered; the temporal worker must run "
            "builtin tool discovery before serving"
        )
    return entry.handler


def _install_worker_approval_callback() -> None:
    """Install the configured non-interactive subagent approval callback on this
    Temporal worker thread, mirroring delegate_task's ThreadPoolExecutor initializer.
    Without it, a subagent's dangerous-command prompt would fall back to input() and
    hang the worker. Default policy is auto-deny (delegation.subagent_auto_approve)."""
    from tools.delegate_tool import _get_subagent_approval_callback
    from tools.terminal_tool import set_approval_callback
    set_approval_callback(_get_subagent_approval_callback())


def execute_durable_step(step: dict) -> dict:
    """Run one durable step as a single subagent delegation. Pure of Temporal."""
    _install_worker_approval_callback()
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


# Temporal activity wrappers — imported lazily so non-temporal runs never import temporalio.
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


def _make_activity():
    """Back-compat: return only the run_step activity (Phase 1 worker used [_make_activity()])."""
    return _make_activities()[0]
