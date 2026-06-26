"""Tests for the temporal kanban spawn fallback path.

NOTE: NO module-level ``pytest.importorskip("temporalio")`` here on purpose.
The fallback (temporal unreachable -> builtin subprocess spawn) is the safety
net and MUST be provable in environments WITHOUT the temporal extra. The
plugin defers all temporalio imports inside functions, and this test only
monkeypatches ``_start_kanban_workflow`` (made to raise) and
``kanban_db._default_spawn`` (the fallback), so it never needs temporalio.
"""


def test_temporal_spawn_falls_back_on_connect_error(tmp_path, monkeypatch):
    """When _start_kanban_workflow raises, temporal_kanban_spawn falls back to builtin."""
    import plugins.kanban_spawn_temporal as plugin
    from hermes_cli import kanban_db

    SENTINEL_PID = 12345

    monkeypatch.setattr(plugin, "_start_kanban_workflow", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no temporal")))
    monkeypatch.setattr(kanban_db, "_default_spawn", lambda task, ws, board=None: SENTINEL_PID)

    class FakeTask:
        id = "t-fallback"
        current_run_id = 1
        assignee = "default"
        tenant = None

    result = plugin.temporal_kanban_spawn(FakeTask(), str(tmp_path), board=None)
    assert result == SENTINEL_PID
