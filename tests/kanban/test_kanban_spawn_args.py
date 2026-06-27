"""Tests for the extracted build_spawn_args / _popen_from_spawn_args helpers."""
from hermes_cli import kanban_db


def _mk_task(**kw):
    import time
    t = kanban_db.Task(
        id="t-1",
        title="x",
        body=None,
        assignee=kw.get("assignee", "default"),
        status="running",
        priority=0,
        created_by=None,
        created_at=int(time.time()),
        started_at=None,
        completed_at=None,
        workspace_kind=kw.get("workspace_kind", "shared"),
        workspace_path=None,
        claim_lock=kw.get("claim_lock", "host:abc"),
        claim_expires=None,
        tenant=None,
    )
    t.current_run_id = kw.get("current_run_id", 7)
    t.max_runtime_seconds = kw.get("max_runtime_seconds", 1800)
    return t


def test_build_spawn_args_overlay_excludes_full_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("SOME_UNRELATED_VAR", "leak-me")
    task = _mk_task()
    args = kanban_db.build_spawn_args(task, str(tmp_path), board=None)
    # Overlay carries kanban-specific keys but NOT arbitrary host env.
    assert args["env_overlay"]["HERMES_KANBAN_TASK"] == "t-1"
    assert "SOME_UNRELATED_VAR" not in args["env_overlay"]
    # argv invokes `chat -q "work kanban task t-1"`.
    assert args["argv"][-2:] == ["-q", "work kanban task t-1"]
    assert args["max_runtime_seconds"] == 1800
    # JSON-serializable.
    import json
    json.dumps(args)


def test_default_spawn_uses_build_then_popen(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(args):
        class P:  # minimal stand-in
            pid = 4321
        captured["args"] = args
        return P()

    monkeypatch.setattr(kanban_db, "_popen_from_spawn_args", fake_popen)
    task = _mk_task()
    pid = kanban_db._default_spawn(task, str(tmp_path), board=None)
    assert pid == 4321
    assert captured["args"]["env_overlay"]["HERMES_KANBAN_TASK"] == "t-1"
