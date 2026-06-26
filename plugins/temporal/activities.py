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
    # Forward the delegation parameters the subagent runner honours so a durable
    # delegation actually runs with the requested context/toolsets/role instead
    # of silently falling back to defaults. (delegate_task ignores model/sub_agent,
    # so we don't forward those.)
    call = {"goal": step["prompt"], "sub_agent": step.get("sub_agent")}
    for key in ("context", "toolsets", "role"):
        if step.get(key) is not None:
            call[key] = step[key]
    raw = handler(call)
    text = raw if isinstance(raw, str) else json.dumps(raw)
    try:
        parsed = json.loads(text)
        ok = parsed.get("status") == "success"
        result = parsed.get("result", text)
    except Exception:
        ok, result = True, text
    return {"name": step.get("name", ""), "ok": ok, "result": result}


def _make_run_kanban_worker(heartbeat=None, sleep=None):
    """Factory so the poll loop is unit-testable without a Temporal context.

    Returns a blocking callable ``(payload: dict) -> dict`` that:
    - Popens the card's subprocess via ``kanban_db._popen_from_spawn_args``
    - Heartbeats while the process runs
    - Reaps the exit code via ``kanban_db.reap_temporal_worker``
    """
    import time as _time
    from hermes_cli import kanban_db

    _sleep = sleep if sleep is not None else _time.sleep

    def _run(payload: dict) -> dict:
        _hb = heartbeat
        if _hb is None:
            from temporalio import activity  # type: ignore
            _hb = activity.heartbeat
        task_id = payload["task_id"]
        board = payload.get("board")
        poll = int(payload.get("poll_seconds", 5))
        proc = kanban_db._popen_from_spawn_args(payload["spawn_args"])
        while proc.poll() is None:
            _hb({"task_id": task_id, "pid": getattr(proc, "pid", None)})
            _sleep(poll)
        exit_code = int(proc.poll() or 0)
        conn = kanban_db.connect(board=board)
        try:
            reap = kanban_db.reap_temporal_worker(conn, task_id, exit_code, board=board)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return {"exit_code": exit_code, "reap": reap}

    return _run


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

    @activity.defn(name="fire_cron_job")
    async def fire_cron_job_activity(job_id: str) -> bool:
        import asyncio as _a
        def _fire() -> bool:
            from tools.registry import discover_builtin_tools
            discover_builtin_tools()  # cron jobs build agents/tools
            # Inline the shared fire_due default body (the ABC can't be instantiated):
            # claim CAS (at-most-once across machines) then run via the shared path.
            from cron.jobs import claim_job_for_fire, get_job
            from cron.scheduler import run_one_job
            if not claim_job_for_fire(job_id):
                return False  # another machine/retry already claimed this fire
            job = get_job(job_id)
            if job is None:
                return False  # job removed between arm and fire
            return run_one_job(job)
        return await _a.to_thread(_fire)

    @activity.defn(name="run_kanban_worker")
    async def run_kanban_worker_activity(payload: dict) -> dict:
        import time as _t
        return await asyncio.to_thread(
            _make_run_kanban_worker(heartbeat=activity.heartbeat, sleep=_t.sleep),
            payload,
        )

    return [run_step_activity, record_outbox_activity, fire_cron_job_activity, run_kanban_worker_activity]


def _make_activity():
    """Back-compat: return only the run_step activity (Phase 1 worker used [_make_activity()])."""
    return _make_activities()[0]
