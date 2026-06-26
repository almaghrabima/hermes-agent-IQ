# tests/plugins/temporal/test_durable_ask.py
import json
from plugins.temporal import tools

class _FakeHandle:
    id = "durable-ask-abc"
class _FakeClient:
    async def start_workflow(self, *a, **kw):
        assert kw.get("task_queue"); return _FakeHandle()

def test_durable_ask_returns_waiting(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(tools, "load_config", lambda: {"temporal": {"enabled": True, "target": "localhost:7233", "namespace": "default", "task_queue": "hermes"}})
    async def fake_connect(s): return _FakeClient()
    monkeypatch.setattr(tools, "connect", fake_connect)
    monkeypatch.setattr("tools.approval.get_current_session_key", lambda default="default": "sessA")
    out = json.loads(tools.handle_durable_ask({"prompt": "Approve? (yes/no)", "choices": ["yes", "no"]}))
    assert out["status"] == "waiting"
    assert out["run_id"] == "durable-ask-abc"
    # waiting notice persisted for the session
    from plugins.temporal import outbox
    assert outbox.get_row("durable-ask-abc:waiting")["session_key"] == "sessA"

def test_durable_ask_requires_prompt():
    out = json.loads(tools.handle_durable_ask({}))
    assert out["status"] == "error"
    assert "prompt" in out["error"]
