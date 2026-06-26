from __future__ import annotations
from typing import Any

SCHEDULE_PREFIX = "hermes-cron-"


def schedule_id(job_id: str) -> str:
    return f"{SCHEDULE_PREFIX}{job_id}"


def job_to_spec(job: dict) -> dict[str, Any]:
    """Temporalio-free description of the Temporal Schedule for a cron job.
    The provider's client_ops translates this into temporalio Schedule objects."""
    sched = job.get("schedule") or {}
    kind = sched.get("kind")
    from cron.jobs import _compute_grace_seconds  # bounded catch-up
    # Resolve the configured Hermes timezone so that recurring cron jobs fire
    # at the correct wall-clock time, matching the built-in ticker.  Jobs have
    # no per-job ``timezone`` key; the timezone is a global Hermes config
    # setting.  Using ``job.get("timezone")`` always returned None → UTC,
    # causing non-UTC deployments to fire at the wrong time.
    import hermes_time as _hermes_time
    _configured_tz: str = _hermes_time._resolve_timezone_name() or "UTC"
    out: dict[str, Any] = {
        "kind": kind,
        "overlap": "skip",
        "catchup_seconds": int(_compute_grace_seconds(sched)),
        "time_zone": _configured_tz,
    }
    if kind == "cron":
        out["cron"] = sched["expr"]
    elif kind == "interval":
        out["every_seconds"] = int(sched["minutes"]) * 60
    elif kind == "once":
        out["run_at"] = sched["run_at"]
        out["remaining_actions"] = 1
    else:
        raise ValueError(f"unsupported cron schedule kind: {kind!r}")
    return out


def plan_reconcile(jobs: list[dict], existing_ids: set[str]) -> dict[str, list]:
    """Diff desired (enabled jobs) vs existing Temporal schedule ids.
    Returns {"upsert": [job_id...], "delete": [schedule_id...]}.
    Disabled jobs are removed from Temporal (no schedule) rather than paused."""
    desired_enabled = {j["id"] for j in jobs if j.get("enabled", True)}
    desired_ids = {schedule_id(jid) for jid in desired_enabled}
    delete = sorted(sid for sid in existing_ids
                    if sid.startswith(SCHEDULE_PREFIX) and sid not in desired_ids)
    return {"upsert": sorted(desired_enabled), "delete": delete}
