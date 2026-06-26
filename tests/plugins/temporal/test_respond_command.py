import types
import plugins.temporal as tp
from plugins.temporal import worker


def test_respond_command_parses_and_signals(monkeypatch):
    calls = {}
    def fake_signal(run_id, answer, session_key):
        calls.update(run_id=run_id, answer=answer, session_key=session_key)
        return {"status": "ok", "run_id": run_id}
    monkeypatch.setattr("plugins.temporal.tools.signal_human_input", fake_signal)
    monkeypatch.setattr("tools.approval.get_current_session_key", lambda default="default": "sessA")
    out = tp._respond_command('durable-ask-abc "yes please"')
    assert calls["run_id"] == "durable-ask-abc"
    assert calls["answer"] == "yes please"
    assert calls["session_key"] == "sessA"
    assert "ok" in out.lower() or "durable-ask-abc" in out


def test_respond_command_usage_when_missing_args():
    out = tp._respond_command("")
    assert "usage" in out.lower()


def test_cmd_temporal_respond_uses_trusted(monkeypatch, capsys):
    """worker.cmd_temporal respond must call signal_human_input with trusted=True."""
    captured = {}

    def fake_signal(run_id, answer, session_key, *, trusted=False):
        captured.update(run_id=run_id, answer=answer, session_key=session_key, trusted=trusted)
        return {"status": "ok", "run_id": run_id}

    monkeypatch.setattr("plugins.temporal.tools.signal_human_input", fake_signal)

    args = types.SimpleNamespace(temporal_command="respond", run_id="durable-ask-xyz", answer="42")
    rc = worker.cmd_temporal(args)

    assert rc == 0
    assert captured["run_id"] == "durable-ask-xyz"
    assert captured["answer"] == "42"
    assert captured["trusted"] is True, "local CLI respond must pass trusted=True to bypass session authz"
