"""TDD tests for the durable=True branch of rlm_tool."""
import json
import tools.rlm_tool as rlm_mod


def test_durable_requires_temporal_enabled(monkeypatch):
    # temporal disabled -> error, NO dispatch.
    monkeypatch.setattr(rlm_mod, "_temporal_enabled", lambda: False)
    called = {"n": 0}
    monkeypatch.setattr(rlm_mod, "_dispatch_durable_rlm",
                        lambda **kw: called.__setitem__("n", called["n"] + 1) or {"status": "dispatched", "run_id": "x"})
    out = json.loads(rlm_mod.rlm_tool(query="q", durable=True))
    assert out["status"] == "error"
    assert "temporal" in out["error"].lower()
    assert called["n"] == 0


def test_durable_dispatches_with_session_and_args(monkeypatch):
    monkeypatch.setattr(rlm_mod, "_temporal_enabled", lambda: True)
    monkeypatch.setattr(rlm_mod, "_current_session_key", lambda: "sess-9")
    seen = {}
    def fake_dispatch(**kw):
        seen.update(kw)
        return {"status": "dispatched", "run_id": "durable-rlm-1"}
    monkeypatch.setattr(rlm_mod, "_dispatch_durable_rlm", fake_dispatch)
    out = json.loads(rlm_mod.rlm_tool(query="big-q", context="ctx", durable=True))
    assert out["status"] == "dispatched"
    assert out["run_id"] == "durable-rlm-1"
    assert seen["session_key"] == "sess-9"
    assert seen["rlm_args"]["query"] == "big-q"
    assert seen["rlm_args"]["context"] == "ctx"
    assert seen["max_attempts"] == 2  # _RLM_CONFIG_DEFAULTS default
