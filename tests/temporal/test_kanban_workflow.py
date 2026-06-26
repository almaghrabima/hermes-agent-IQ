"""Tests for the run_kanban_worker activity blocking core.

The pytest.importorskip at module level means this file skips cleanly when
temporalio is not installed. The blocking-core helper (_make_run_kanban_worker)
itself does not need temporalio when heartbeat/sleep are injected — only the
Temporal-registered wrapper does.
"""
import pytest
pytest.importorskip("temporalio")


def test_run_kanban_worker_popens_and_reaps(tmp_path, monkeypatch):
    from plugins.temporal import activities as A
    from hermes_cli import kanban_db

    calls = {}

    class FakeProc:
        pid = 999

        def __init__(self):
            self._n = 0

        def poll(self):
            self._n += 1
            return None if self._n < 2 else 0  # alive once, then exit 0

    monkeypatch.setattr(kanban_db, "_popen_from_spawn_args", lambda args: FakeProc())
    def fake_reap(conn, tid, code, **kw):
        calls["reap"] = (tid, code)
        return "terminal"

    monkeypatch.setattr(kanban_db, "reap_temporal_worker", fake_reap)
    monkeypatch.setattr(kanban_db, "connect", lambda *a, **k: object())

    run = A._make_run_kanban_worker(heartbeat=lambda *a, **k: None, sleep=lambda s: None)
    out = run({"task_id": "t-1", "spawn_args": {"argv": []}, "board": None, "poll_seconds": 0})
    assert out["exit_code"] == 0
    assert out["reap"] == "terminal"
    assert calls["reap"] == ("t-1", 0)


def test_kanban_workflow_retry_tracks_failure_limit():
    """_kanban_retry_policy(failure_limit=3) should allow 4 total attempts."""
    from plugins.temporal import workflows
    policy = workflows._kanban_retry_policy(failure_limit=3)
    assert policy.maximum_attempts == 4


@pytest.mark.asyncio
async def test_kanban_workflow_runs_activity_and_completes(monkeypatch):
    """End-to-end: KanbanTaskWorkflow → run_kanban_worker activity (subprocess stubbed)."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from plugins.temporal import activities as A
    from plugins.temporal.workflows import _make_kanban_task_workflow
    from hermes_cli import kanban_db

    # Stub the subprocess: exits 0 immediately; avoids any real Popen.
    class FakeProc:
        pid = 1

        def poll(self):
            return 0  # already exited

    monkeypatch.setattr(kanban_db, "_popen_from_spawn_args", lambda args: FakeProc())
    monkeypatch.setattr(kanban_db, "connect", lambda *a, **k: object())
    seen = {}
    monkeypatch.setattr(
        kanban_db,
        "reap_temporal_worker",
        lambda conn, tid, code, **kw: seen.setdefault("code", code) or "terminal",
    )

    WF = _make_kanban_task_workflow()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[WF],
            activities=A._make_activities(),
        ):
            result = await env.client.execute_workflow(
                "KanbanTaskWorkflow",
                {
                    "task_id": "t-1",
                    "spawn_args": {"argv": [], "max_runtime_seconds": 10},
                    "board": None,
                    "failure_limit": 2,
                    "poll_seconds": 0,
                },
                id="hermes-kanban-t-1-1",
                task_queue="tq",
            )
    assert result["exit_code"] == 0
    assert result["reap"] == "terminal"
    assert seen["code"] == 0


@pytest.mark.asyncio
async def test_kanban_workflow_finalizes_card_on_activity_failure(monkeypatch):
    """Regression: when _popen_from_spawn_args raises (terminal activity failure),
    KanbanTaskWorkflow must call reap_failed_kanban_worker to finalize the card
    instead of orphaning it in 'running' status (SQLite reapers skip temporal rows)."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from plugins.temporal import activities as A
    from plugins.temporal.workflows import _make_kanban_task_workflow
    from hermes_cli import kanban_db

    # Stub Popen to raise — simulates missing hermes binary or broken environment.
    monkeypatch.setattr(
        kanban_db, "_popen_from_spawn_args",
        lambda args: (_ for _ in ()).throw(RuntimeError("no hermes")),
    )
    monkeypatch.setattr(kanban_db, "connect", lambda *a, **k: object())
    reap_calls = []
    monkeypatch.setattr(
        kanban_db, "reap_temporal_worker",
        lambda conn, tid, code, **kw: reap_calls.append({"tid": tid, "code": code}) or "failed",
    )

    WF = _make_kanban_task_workflow()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq2",
            workflows=[WF],
            activities=A._make_activities(),
        ):
            with pytest.raises(Exception):
                await env.client.execute_workflow(
                    "KanbanTaskWorkflow",
                    {
                        "task_id": "t-fail",
                        "spawn_args": {"argv": [], "max_runtime_seconds": 10},
                        "board": None,
                        "failure_limit": 0,  # no retries — fail fast
                        "poll_seconds": 0,
                    },
                    id="hermes-kanban-t-fail-1",
                    task_queue="tq2",
                )
    # The finalizer activity must have been called for the failing task.
    assert any(c["tid"] == "t-fail" for c in reap_calls), (
        "reap_temporal_worker was never called — card would be orphaned 'running'"
    )
