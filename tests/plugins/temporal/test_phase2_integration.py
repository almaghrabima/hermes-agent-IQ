# tests/plugins/temporal/test_phase2_integration.py
import uuid
import pytest
pytest.importorskip("temporalio")
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from plugins.temporal.worker import build_workflow_runner
from temporalio import activity
from plugins.temporal.workflows import _make_background_workflow
from plugins.temporal import outbox, delivery

pytestmark = pytest.mark.integration

_CAPTURED_STEP: dict = {}

@activity.defn(name="run_step")
async def ok_step(step: dict) -> dict:
    _CAPTURED_STEP.clear()
    _CAPTURED_STEP.update(step)
    return {"name": step.get("name", ""), "ok": True, "result": "answer"}

@activity.defn(name="record_outbox")
async def real_record(payload: dict) -> None:
    outbox.record_completion(payload["run_id"], payload["session_key"], payload["status"], payload["block"])

@activity.defn(name="run_step")
async def boom_step(step: dict) -> dict:
    raise RuntimeError("subagent blew up")

@pytest.mark.asyncio
async def test_durable_delegation_delivers_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"hermes-p2-{uuid.uuid4().hex[:8]}"
        run_id = f"durable-deleg-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=tq,
                          workflows=[_make_background_workflow()], activities=[ok_step, real_record],
                          workflow_runner=build_workflow_runner()):
            wf_result = await env.client.execute_workflow(
                "BackgroundDelegationWorkflow",
                {"goal": "q", "session_key": "sessA", "run_id": run_id,
                 "context": "ctx facts", "toolsets": ["web"], "role": "orchestrator"},
                id=run_id, task_queue=tq)
    # Regression: the workflow must pass the delegation params down into the
    # step so the subagent actually runs with the requested context/toolsets/role.
    assert _CAPTURED_STEP.get("context") == "ctx facts", (
        f"Expected context forwarded into step, got: {_CAPTURED_STEP}")
    assert _CAPTURED_STEP.get("toolsets") == ["web"], (
        f"Expected toolsets forwarded into step, got: {_CAPTURED_STEP}")
    assert _CAPTURED_STEP.get("role") == "orchestrator", (
        f"Expected role forwarded into step, got: {_CAPTURED_STEP}")
    # FIX 1: workflow return value must carry session_key and the real block
    assert wf_result.get("session_key") == "sessA", (
        f"Expected session_key='sessA' in workflow result, got: {wf_result}"
    )
    assert "block" in wf_result, f"Expected 'block' key in workflow result, got: {wf_result}"
    block = wf_result["block"]
    assert block.get("summary") == "answer", (
        f"Expected block['summary']='answer' (real result), got: {block}"
    )
    # "restart": a fresh delivery call (new process) finds the outbox row
    events = delivery.drain_outbox_for_sessions(["sessA"])
    assert len(events) == 1
    assert events[0]["type"] == "async_delegation"
    assert events[0]["session_key"] == "sessA"
    assert events[0]["summary"] == "answer"
    assert delivery.drain_outbox_for_sessions(["sessA"]) == []  # exactly once


@pytest.mark.asyncio
async def test_durable_delegation_failure_is_recorded(tmp_path, monkeypatch):
    """A durable delegation whose activity exhausts its retries must still record
    a 'failed' outbox row so the user learns it failed — not vanish silently."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        tq = f"hermes-p2f-{uuid.uuid4().hex[:8]}"
        run_id = f"durable-deleg-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=tq,
                          workflows=[_make_background_workflow()], activities=[boom_step, real_record],
                          workflow_runner=build_workflow_runner()):
            wf_result = await env.client.execute_workflow(
                "BackgroundDelegationWorkflow",
                {"goal": "q", "session_key": "sessF", "run_id": run_id,
                 "retry": {"max_attempts": 1}},
                id=run_id, task_queue=tq)
    assert wf_result["status"] == "failed", f"expected failed status, got {wf_result}"
    assert wf_result["block"]["error"], f"expected an error in block, got {wf_result['block']}"
    events = delivery.drain_outbox_for_sessions(["sessF"])
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert events[0]["error"]
