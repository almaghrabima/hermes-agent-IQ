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
