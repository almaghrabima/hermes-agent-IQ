# tests/plugins/temporal/test_integration.py
import uuid
import pytest

pytest.importorskip("temporalio")
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402
from plugins.temporal.workflows import _make_workflow  # noqa: E402
from temporalio import activity  # noqa: E402

pytestmark = pytest.mark.integration

_attempts = {"n": 0}

@activity.defn(name="run_step")
async def flaky_run_step(step: dict) -> dict:
    # fail twice, then succeed — proves RetryPolicy drives it to completion
    _attempts["n"] += 1
    if _attempts["n"] < 3:
        raise RuntimeError("transient")
    return {"name": step.get("name", ""), "ok": True, "result": "done"}

@pytest.mark.asyncio
async def test_workflow_retries_then_completes():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"hermes-test-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=tq,
                          workflows=[_make_workflow()], activities=[flaky_run_step]):
            result = await env.client.execute_workflow(
                "DurableRunWorkflow",
                {"steps": [{"name": "s1", "prompt": "x"}],
                 "retry": {"max_attempts": 5, "initial_interval_seconds": 1}},
                id=f"it-{uuid.uuid4().hex[:8]}", task_queue=tq)
    assert result["completed"] == 1
    assert result["steps"][0]["ok"] is True
    assert _attempts["n"] == 3  # exactly-once after retries
