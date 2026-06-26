import plugins.temporal.tools as T


def test_dispatch_durable_rlm_starts_workflow(monkeypatch):
    started = {}

    class FakeHandle:
        id = "durable-rlm-abc"

    class FakeClient:
        async def start_workflow(self, name, payload, *, id, task_queue):
            started["name"] = name
            started["payload"] = payload
            started["id"] = id
            return FakeHandle()

    async def fake_connect(s):
        return FakeClient()

    monkeypatch.setattr(T, "connect", fake_connect)
    out = T.dispatch_durable_rlm(
        rlm_args={"query": "q"}, session_key="sess-1", max_attempts=2, timeout_seconds=600)
    assert out["status"] == "dispatched"
    assert out["run_id"].startswith("durable-rlm-")
    assert started["name"] == "RlmRunWorkflow"
    assert started["payload"]["session_key"] == "sess-1"
    assert started["payload"]["rlm_args"] == {"query": "q"}
    assert started["payload"]["max_attempts"] == 2


def test_reconcile_backfills_rlm(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.temporal import delivery, outbox
    import plugins.temporal.tools as T

    monkeypatch.setattr(
        T, "list_completed_durable_rlm",
        lambda: [{"run_id": "durable-rlm-z", "session_key": "s", "status": "completed",
                  "block": {"goal": "q", "summary": "A", "status": "completed"}}])
    # delegation list returns nothing so only rlm is backfilled
    monkeypatch.setattr(T, "list_completed_durable_delegations", lambda: [])

    inserted = delivery.reconcile_from_temporal()
    assert inserted >= 1
    assert outbox.has_run("durable-rlm-z")
