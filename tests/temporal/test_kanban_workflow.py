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
