from plugins.cron_providers.temporal import schedules as S

def _job(jid, sched, enabled=True, tz=None):
    return {"id": jid, "schedule": sched, "enabled": enabled, "timezone": tz}

def test_schedule_id_namespaced():
    assert S.schedule_id("abc") == "hermes-cron-abc"
    assert S.schedule_id("abc").startswith(S.SCHEDULE_PREFIX)

def test_cron_job_maps_to_cron_spec():
    spec = S.job_to_spec(_job("j1", {"kind": "cron", "expr": "0 9 * * *"}, tz="UTC"))
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
