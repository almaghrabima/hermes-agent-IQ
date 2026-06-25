# tests/plugins/temporal/test_activities_registry.py
"""Prove that execute_durable_step finds delegate_task through the real registry path."""
from tools.registry import discover_builtin_tools, registry
from plugins.temporal import activities


def test_discover_registers_delegate_task():
    """discover_builtin_tools populates the real registry with delegate_task."""
    discover_builtin_tools()
    assert "delegate_task" in registry._tools, (
        "delegate_task not found after discover_builtin_tools(); "
        "worker bootstrap would fail at runtime"
    )


def test_execute_durable_step_via_real_registry(monkeypatch):
    """execute_durable_step resolves delegate_task through the real registry lookup."""
    discover_builtin_tools()
    assert "delegate_task" in registry._tools

    # Replace only the handler to avoid a real LLM call.
    original_handler = registry._tools["delegate_task"].handler

    def fake_handler(args, **kw):
        return '{"status": "success", "result": "ok"}'

    monkeypatch.setattr(registry._tools["delegate_task"], "handler", fake_handler)
    try:
        out = activities.execute_durable_step({"name": "s", "prompt": "p"})
    finally:
        monkeypatch.setattr(registry._tools["delegate_task"], "handler", original_handler)

    assert out["ok"] is True
    assert out["result"] == "ok"
