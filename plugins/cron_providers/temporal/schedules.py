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
    out: dict[str, Any] = {
        "kind": kind,
        "overlap": "skip",
        "catchup_seconds": int(_compute_grace_seconds(sched)),
        "time_zone": job.get("timezone") or "UTC",
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
