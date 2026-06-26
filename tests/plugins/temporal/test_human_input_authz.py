# tests/plugins/temporal/test_human_input_authz.py
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from plugins.temporal import tools, outbox


def _patch_connect(monkeypatch):
    """Replace tools.connect with a fake that returns an async client whose
    get_workflow_handle().signal() is an async no-op."""
    fake_handle = MagicMock()
    fake_handle.signal = AsyncMock(return_value=None)
    fake_client = MagicMock()
    fake_client.get_workflow_handle = MagicMock(return_value=fake_handle)

    async def _fake_connect(_s):
        return fake_client

    monkeypatch.setattr("plugins.temporal.tools.connect", _fake_connect)
    return fake_handle


def test_signal_rejects_session_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("run-x:waiting", "owner", "waiting", {"prompt": "?"})
    out = json.loads(json.dumps(tools.signal_human_input("run-x", "yes", session_key="intruder")))
    assert out["status"] == "error"
    assert "authoriz" in out["error"].lower() or "session" in out["error"].lower()


def test_signal_authorized_when_session_matches(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_connect(monkeypatch)
    outbox.record_completion("run-a:waiting", "sessA", "waiting", {"prompt": "ok?"})
    out = tools.signal_human_input("run-a", "yes", "sessA")
    assert out["status"] == "ok"
    assert out["run_id"] == "run-a"


def test_signal_trusted_bypasses_session_check(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_connect(monkeypatch)
    # waiting row belongs to "sessA"; caller supplies "default" but trusted=True
    outbox.record_completion("run-b:waiting", "sessA", "waiting", {"prompt": "ok?"})
    out = tools.signal_human_input("run-b", "yes", "default", trusted=True)
    assert out["status"] == "ok"
    assert out["run_id"] == "run-b"
