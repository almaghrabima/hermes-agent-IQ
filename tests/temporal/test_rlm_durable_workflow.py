import pytest
pytest.importorskip("temporalio")

from plugins.temporal import workflows


def test_rlm_retry_policy_uses_max_attempts():
    assert workflows._rlm_retry_policy(3).maximum_attempts == 3


def test_make_rlm_run_workflow_returns_class():
    assert workflows._make_rlm_run_workflow().__name__ == "RlmRunWorkflow"


@pytest.mark.asyncio
async def test_rlm_workflow_runs_and_delivers(monkeypatch, tmp_path):
    """End-to-end: RlmRunWorkflow → run_rlm_durable (stubbed) → record_outbox → drain."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from plugins.temporal import activities as A
    from plugins.temporal.workflows import _make_rlm_run_workflow
    from plugins.temporal.worker import build_workflow_runner
    from plugins.temporal import delivery
    import tools.rlm_tool as rlm_mod

    monkeypatch.setattr(
        rlm_mod, "rlm_tool",
        lambda **kw: '{"status":"success","result":"DURABLE-ANSWER","usage":{},"log_path":""}',
    )

    import concurrent.futures
    WF = _make_rlm_run_workflow()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # _make_activities() now includes a SYNC activity (run_kanban_worker), so
        # the Worker requires an activity_executor.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            async with Worker(
                env.client,
                task_queue="tq",
                workflows=[WF],
                activities=A._make_activities(),
                activity_executor=pool,
                workflow_runner=build_workflow_runner(),
            ):
                result = await env.client.execute_workflow(
                    "RlmRunWorkflow",
                    {
                        "rlm_args": {"query": "q"},
                        "session_key": "sess-e2e",
                        "run_id": "durable-rlm-e2e",
                        "max_attempts": 2,
                        "timeout_seconds": 30,
                    },
                    id="durable-rlm-e2e",
                    task_queue="tq",
                )
    assert result["status"] == "completed"
    assert result["block"]["summary"] == "DURABLE-ANSWER"
    # It landed in the outbox and drains to the originating session.
    events = delivery.drain_outbox_for_sessions(["sess-e2e"])
    assert any(
        e["delegation_id"] == "durable-rlm-e2e" and e["summary"] == "DURABLE-ANSWER"
        for e in events
    )
