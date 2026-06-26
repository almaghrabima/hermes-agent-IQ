# tests/plugins/temporal/test_human_input_authz.py
import json
from plugins.temporal import tools, outbox

def test_signal_rejects_session_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("run-x:waiting", "owner", "waiting", {"prompt": "?"})
    out = json.loads(json.dumps(tools.signal_human_input("run-x", "yes", session_key="intruder")))
    assert out["status"] == "error"
    assert "authoriz" in out["error"].lower() or "session" in out["error"].lower()
