from hermes_cli import kanban_db
from hermes_cli.kanban_spawn_provider import resolve_kanban_spawn


def test_default_provider_is_builtin():
    fn = resolve_kanban_spawn({"kanban": {}})
    assert fn is kanban_db._default_spawn
    assert getattr(fn, "_kanban_run_kind", None) is None


def test_temporal_selected_but_disabled_falls_back_to_builtin():
    cfg = {"kanban": {"spawn_provider": "temporal"}, "temporal": {"enabled": False}}
    fn = resolve_kanban_spawn(cfg)
    assert fn is kanban_db._default_spawn  # fell back


def test_temporal_selected_and_enabled_returns_tagged_callable(monkeypatch):
    # Stub the plugin import so this test doesn't depend on Task 7 wiring.
    import sys, types
    mod = types.ModuleType("plugins.kanban_spawn_temporal")
    def _spawn(task, workspace, *, board=None):  # noqa: ANN001
        return None
    mod.temporal_kanban_spawn = _spawn
    monkeypatch.setitem(sys.modules, "plugins.kanban_spawn_temporal", mod)
    cfg = {"kanban": {"spawn_provider": "temporal"}, "temporal": {"enabled": True}}
    fn = resolve_kanban_spawn(cfg)
    assert getattr(fn, "_kanban_run_kind", None) == "temporal"


def test_temporal_enabled_but_import_fails_falls_back_to_builtin(monkeypatch):
    import sys
    from hermes_cli import kanban_db
    from hermes_cli.kanban_spawn_provider import resolve_kanban_spawn
    monkeypatch.delitem(sys.modules, "plugins.kanban_spawn_temporal", raising=False)
    cfg = {"kanban": {"spawn_provider": "temporal"}, "temporal": {"enabled": True}}
    fn = resolve_kanban_spawn(cfg)
    assert fn is kanban_db._default_spawn
