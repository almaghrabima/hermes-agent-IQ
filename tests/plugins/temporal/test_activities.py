# tests/plugins/temporal/test_activities.py
from plugins.temporal import activities

def test_execute_durable_step_calls_runner(monkeypatch):
    captured = {}
    def fake_runner(args, **kw):
        captured.update(args)
        return '{"status": "success", "result": "done"}'
    monkeypatch.setattr(activities, "_delegate_handler", lambda: fake_runner)
    out = activities.execute_durable_step({"name": "s1", "prompt": "do x", "sub_agent": "m"})
    assert captured["goal"] == "do x"
    assert out["name"] == "s1"
    assert out["ok"] is True
    assert "done" in out["result"]
