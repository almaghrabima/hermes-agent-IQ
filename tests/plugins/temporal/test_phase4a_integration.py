# tests/plugins/temporal/test_phase4a_integration.py
import uuid, asyncio
import pytest
pytest.importorskip("temporalio")
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity
from plugins.temporal.workflows import _make_cron_fire_workflow

pytestmark = pytest.mark.integration

_fired = []

@activity.defn(name="fire_cron_job")
async def fake_fire(job_id: str) -> bool:
    _fired.append(job_id)
    return True

@pytest.mark.asyncio
async def test_cron_fire_workflow_invokes_fire_activity():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"hermes-p4a-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=tq,
                          workflows=[_make_cron_fire_workflow()], activities=[fake_fire]):
            out = await env.client.execute_workflow(
                "CronFireWorkflow", "job-123", id=f"cf-{uuid.uuid4().hex[:8]}", task_queue=tq)
    assert out is True
    assert _fired == ["job-123"]
