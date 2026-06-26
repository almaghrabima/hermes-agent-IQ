import hermes_time
from plugins.cron_providers.temporal import schedules as S

def _job(jid, sched, enabled=True):
    return {"id": jid, "schedule": sched, "enabled": enabled}

def test_schedule_id_namespaced():
    assert S.schedule_id("abc") == "hermes-cron-abc"
    assert S.schedule_id("abc").startswith(S.SCHEDULE_PREFIX)

def test_cron_job_maps_to_cron_spec(monkeypatch):
    monkeypatch.setattr(hermes_time, "_resolve_timezone_name", lambda: "UTC")
    spec = S.job_to_spec(_job("j1", {"kind": "cron", "expr": "0 9 * * *"}))
    assert spec["kind"] == "cron"
    assert spec["cron"] == "0 9 * * *"
    assert spec["time_zone"] == "UTC"
    assert spec["overlap"] == "skip"

def test_interval_job_maps_minutes_to_seconds():
    spec = S.job_to_spec(_job("j2", {"kind": "interval", "minutes": 30}))
    assert spec["kind"] == "interval"
    assert spec["every_seconds"] == 1800

def test_once_job_maps_to_single_action():
    spec = S.job_to_spec(_job("j3", {"kind": "once", "run_at": "2026-07-01T09:00:00+00:00"}))
    assert spec["kind"] == "once"
    assert spec["run_at"] == "2026-07-01T09:00:00+00:00"
    assert spec["remaining_actions"] == 1

def test_plan_reconcile_upserts_enabled_deletes_orphans():
    jobs = [_job("a", {"kind": "interval", "minutes": 5}),
            _job("b", {"kind": "cron", "expr": "* * * * *"}, enabled=False)]
    existing = {"hermes-cron-a", "hermes-cron-gone"}
    plan = S.plan_reconcile(jobs, existing)
    # enabled job 'a' upserted; disabled 'b' not upserted (paused via delete);
    # orphan 'hermes-cron-gone' + disabled 'b' schedule deleted
    assert "a" in plan["upsert"]
    assert "b" not in plan["upsert"]
    assert "hermes-cron-gone" in plan["delete"]


# ── Timezone correctness ──────────────────────────────────────────────────────

def test_cron_uses_configured_timezone(monkeypatch):
    """job_to_spec must use the CONFIGURED Hermes timezone, not a per-job key."""
    monkeypatch.setattr(hermes_time, "_resolve_timezone_name", lambda: "America/New_York")
    spec = S.job_to_spec(_job("j4", {"kind": "cron", "expr": "0 9 * * *"}))
    assert spec["time_zone"] == "America/New_York"


def test_cron_defaults_utc_when_unset(monkeypatch):
    """job_to_spec must fall back to UTC when no timezone is configured."""
    monkeypatch.setattr(hermes_time, "_resolve_timezone_name", lambda: "")
    spec = S.job_to_spec(_job("j5", {"kind": "cron", "expr": "0 9 * * *"}))
    assert spec["time_zone"] == "UTC"
