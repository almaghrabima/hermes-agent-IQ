from __future__ import annotations
from datetime import datetime, timedelta
from plugins.cron_providers.temporal.schedules import job_to_spec, schedule_id, SCHEDULE_PREFIX


def build_schedule(spec: dict, *, job_id: str, task_queue: str):
    """Translate a temporalio-free spec dict (job_to_spec) into a temporalio Schedule."""
    from temporalio.client import (  # type: ignore
        Schedule, ScheduleActionStartWorkflow, ScheduleSpec, ScheduleIntervalSpec,
        ScheduleCalendarSpec, ScheduleRange, SchedulePolicy, ScheduleOverlapPolicy,
        ScheduleState,
    )
    action = ScheduleActionStartWorkflow(
        "CronFireWorkflow", args=[job_id],
        id=f"cronfire-{job_id}", task_queue=task_queue,
    )
    kind = spec["kind"]
    if kind == "cron":
        sspec = ScheduleSpec(cron_expressions=[spec["cron"]], time_zone_name=spec.get("time_zone", "UTC"))
        state = ScheduleState()
    elif kind == "interval":
        sspec = ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=spec["every_seconds"]))])
        state = ScheduleState()
    elif kind == "once":
        # One-shots represent an absolute instant.  Convert to UTC before
        # extracting calendar fields so that a run_at with a non-UTC offset
        # (e.g. "2026-07-01T09:00:00-04:00") fires at the correct UTC wall
        # clock time (13:00 UTC) rather than the bare local hour (09:00 UTC).
        from datetime import timezone as _tz
        dt = datetime.fromisoformat(spec["run_at"])
        if dt.tzinfo is not None:
            dt = dt.astimezone(_tz.utc)
        cal = ScheduleCalendarSpec(
            year=[ScheduleRange(dt.year)], month=[ScheduleRange(dt.month)],
            day_of_month=[ScheduleRange(dt.day)], hour=[ScheduleRange(dt.hour)],
            minute=[ScheduleRange(dt.minute)],
        )
        sspec = ScheduleSpec(calendars=[cal], time_zone_name="UTC")
        state = ScheduleState(limited_actions=True, remaining_actions=int(spec.get("remaining_actions", 1)))
    else:
        raise ValueError(f"unsupported kind {kind!r}")
    policy = SchedulePolicy(
        overlap=ScheduleOverlapPolicy.SKIP,
        catchup_window=timedelta(seconds=int(spec.get("catchup_seconds", 60))),
    )
    return Schedule(action=action, spec=sspec, policy=policy, state=state)


async def upsert_schedule(job: dict) -> None:
    from plugins.temporal.client import connect
    from plugins.temporal.tconfig import resolve_temporal_config
    from hermes_cli.config import load_config
    from temporalio.client import ScheduleAlreadyRunningError  # type: ignore
    s = resolve_temporal_config(load_config())
    client = await connect(s)
    sid = schedule_id(job["id"])
    sched = build_schedule(job_to_spec(job), job_id=job["id"], task_queue=s.task_queue)
    try:
        await client.create_schedule(sid, sched)
    except ScheduleAlreadyRunningError:
        # Already exists → update (idempotent create-or-update).
        handle = client.get_schedule_handle(sid)
        await handle.delete()
        await client.create_schedule(sid, sched)


async def delete_schedule(sid: str) -> None:
    from plugins.temporal.client import connect
    from plugins.temporal.tconfig import resolve_temporal_config
    from hermes_cli.config import load_config
    s = resolve_temporal_config(load_config())
    client = await connect(s)
    try:
        await client.get_schedule_handle(sid).delete()
    except Exception:
        pass  # already gone


async def list_hermes_schedule_ids() -> set[str]:
    from plugins.temporal.client import connect
    from plugins.temporal.tconfig import resolve_temporal_config
    from hermes_cli.config import load_config
    s = resolve_temporal_config(load_config())
    client = await connect(s)
    out: set[str] = set()
    async for desc in await client.list_schedules():
        if desc.id.startswith(SCHEDULE_PREFIX):
            out.add(desc.id)
    return out
