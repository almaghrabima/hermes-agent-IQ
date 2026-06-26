import json
import tools.delegate_tool as dt


def test_durable_requires_temporal_enabled(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"temporal": {"enabled": False}})
    out = json.loads(dt.delegate_task(goal="g", background=True, durable=True))
    assert out["status"] == "error"
    assert "temporal" in out["error"].lower()


def test_durable_routes_to_temporal_dispatch(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"temporal": {"enabled": True}})
    calls = {}
    def fake_dispatch(**kw):
        calls.update(kw); return {"status": "dispatched", "run_id": "durable-deleg-xyz"}
    monkeypatch.setattr("plugins.temporal.tools.dispatch_durable_delegation", fake_dispatch)
    out = json.loads(dt.delegate_task(goal="do x", background=True, durable=True))
    assert out["status"] == "dispatched"
    assert out["run_id"] == "durable-deleg-xyz"
    assert calls["goal"] == "do x"


def test_durable_without_background_errors(monkeypatch):
    """durable=True with background=False must return an explicit error."""
    monkeypatch.setattr(dt, "_load_config", lambda: {"temporal": {"enabled": True}})
    out = json.loads(dt.delegate_task(goal="g", background=False, durable=True))
    assert out["status"] == "error"
    assert "background" in out["error"].lower()


def test_durable_rejects_batch(monkeypatch):
    """durable=True with tasks=[...] must reject with a batch error."""
    monkeypatch.setattr(dt, "_load_config", lambda: {"temporal": {"enabled": True}})
    out = json.loads(dt.delegate_task(
        goal=None,
        background=True,
        durable=True,
        tasks=[{"goal": "a"}],
    ))
    assert out["status"] == "error"
    assert "batch" in out["error"].lower()
