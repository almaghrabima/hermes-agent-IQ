# tests/plugins/temporal/test_phase2_integration.py
import uuid
import pytest
pytest.importorskip("temporalio")
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity
from plugins.temporal.workflows import _make_background_workflow
from plugins.temporal import outbox, delivery

pytestmark = pytest.mark.integration

@activity.defn(name="run_step")
async def ok_step(step: dict) -> dict:
    return {"name": step.get("name", ""), "ok": True, "result": "answer"}

@activity.defn(name="record_outbox")
async def real_record(payload: dict) -> None:
    outbox.record_completion(payload["run_id"], payload["session_key"], payload["status"], payload["block"])

@pytest.mark.asyncio
async def test_durable_delegation_delivers_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"hermes-p2-{uuid.uuid4().hex[:8]}"
        run_id = f"durable-deleg-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=tq,
                          workflows=[_make_background_workflow()], activities=[ok_step, real_record]):
            await env.client.execute_workflow(
                "BackgroundDelegationWorkflow",
                {"goal": "q", "session_key": "sessA", "run_id": run_id},
                id=run_id, task_queue=tq)
    # "restart": a fresh delivery call (new process) finds the outbox row
    events = delivery.drain_outbox_for_sessions(["sessA"])
    assert len(events) == 1
    assert events[0]["type"] == "async_delegation"
    assert events[0]["session_key"] == "sessA"
    assert events[0]["summary"] == "answer"
    assert delivery.drain_outbox_for_sessions(["sessA"]) == []  # exactly once
