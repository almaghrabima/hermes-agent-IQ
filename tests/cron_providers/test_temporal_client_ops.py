import pytest
pytest.importorskip("temporalio")
from plugins.cron_providers.temporal import client_ops as C


def test_build_schedule_cron():
    sched = C.build_schedule(
        {"kind": "cron", "cron": "0 9 * * *", "overlap": "skip",
         "catchup_seconds": 60, "time_zone": "UTC"},
        job_id="j1", task_queue="hermes")
    # action targets the CronFireWorkflow with the job_id arg
    assert sched.action.workflow == "CronFireWorkflow"
    assert sched.action.args == ["j1"]
    assert sched.spec.cron_expressions == ["0 9 * * *"]


def test_build_schedule_interval():
    sched = C.build_schedule(
        {"kind": "interval", "every_seconds": 1800, "overlap": "skip",
         "catchup_seconds": 60, "time_zone": "UTC"},
        job_id="j2", task_queue="hermes")
    assert sched.spec.intervals[0].every.total_seconds() == 1800


def test_build_schedule_once_is_limited():
    sched = C.build_schedule(
        {"kind": "once", "run_at": "2026-07-01T09:00:00+00:00", "remaining_actions": 1,
         "overlap": "skip", "catchup_seconds": 60, "time_zone": "UTC"},
        job_id="j3", task_queue="hermes")
    assert sched.state.limited_actions is True
    assert sched.state.remaining_actions == 1


def test_once_honors_run_at_offset():
    """A run_at with a non-UTC offset must fire at the correct UTC instant.

    2026-07-01T09:00:00-04:00 == 13:00 UTC.  The calendar hour stored in the
    Temporal schedule must be 13 (UTC), not 9 (the bare local hour).
    """
    sched = C.build_schedule(
        {"kind": "once", "run_at": "2026-07-01T09:00:00-04:00", "remaining_actions": 1,
         "overlap": "skip", "catchup_seconds": 60, "time_zone": "UTC"},
        job_id="j4", task_queue="hermes")
    assert sched.spec.calendars[0].hour[0].start == 13
    assert sched.spec.time_zone_name == "UTC"
