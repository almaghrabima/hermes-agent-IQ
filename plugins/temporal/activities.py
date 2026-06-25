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
