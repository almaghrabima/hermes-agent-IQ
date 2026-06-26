# tests/plugins/temporal/test_phase3_integration.py
import uuid, asyncio
import pytest
pytest.importorskip("temporalio")
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity
from plugins.temporal.workflows import _make_human_input_workflow
from plugins.temporal import outbox

pytestmark = pytest.mark.integration

@activity.defn(name="record_outbox")
async def real_record(payload: dict) -> None:
    outbox.record_completion(payload["run_id"], payload["session_key"], payload["status"], payload["block"])

@pytest.mark.asyncio
async def test_signal_resumes_and_delivers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"hermes-p3-{uuid.uuid4().hex[:8]}"; run_id = f"durable-ask-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=tq,
                          workflows=[_make_human_input_workflow()], activities=[real_record]):
            h = await env.client.start_workflow(
                "HumanInputWorkflow",
                {"prompt": "ok?", "session_key": "sessA", "run_id": run_id, "timeout_seconds": 3600},
                id=run_id, task_queue=tq)
            await h.signal("respond", "yes")
            res = await h.result()
    assert res["status"] == "answered"
    assert outbox.get_row(run_id)["block"]["summary"] == "yes"

@pytest.mark.asyncio
async def test_timeout_completes_timed_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"hermes-p3-{uuid.uuid4().hex[:8]}"; run_id = f"durable-ask-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=tq,
                          workflows=[_make_human_input_workflow()], activities=[real_record]):
            res = await env.client.execute_workflow(
                "HumanInputWorkflow",
                {"prompt": "ok?", "session_key": "sessA", "run_id": run_id, "timeout_seconds": 1},
                id=run_id, task_queue=tq)
    assert res["status"] == "timed_out"
    assert res["block"]["summary"] is None
