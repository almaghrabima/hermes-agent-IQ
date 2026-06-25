# tests/plugins/temporal/test_tools.py
import json
from plugins.temporal import tools

class _FakeHandle:
    id = "run-123"
    async def result(self):
        return {"steps": [{"name": "s1", "ok": True, "result": "done"}], "completed": 1}

class _FakeClient:
    async def start_workflow(self, *a, **kw):
        return _FakeHandle()

def test_durable_run_returns_completed(monkeypatch):
    async def fake_connect(s):
        return _FakeClient()
    monkeypatch.setattr(tools, "connect", fake_connect)
    out = json.loads(tools.handle_durable_run(
        {"steps": [{"name": "s1", "prompt": "do x"}], "wait_seconds": 5}))
    assert out["status"] == "completed"
    assert out["run_id"] == "run-123"
    assert out["result"]["completed"] == 1

def test_durable_run_arg_validation():
    out = json.loads(tools.handle_durable_run({"steps": []}))
    assert out["status"] == "error"
    assert "steps" in out["error"]
