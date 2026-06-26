# tests/temporal/test_rlm_durable_activity.py
from plugins.temporal import activities as A


def test_run_rlm_blocking_success(monkeypatch):
    import tools.rlm_tool as rlm_mod
    monkeypatch.setattr(
        rlm_mod, "rlm_tool",
        lambda **kw: '{"status": "success", "result": "ANSWER", "usage": {"calls": 3}, "log_path": "/x.log"}')
    out = A._run_rlm_blocking({"rlm_args": {"query": "q"}})
    assert out["ok"] is True
    assert out["summary"] == "ANSWER"
    assert out["error"] is None
    assert out["usage"] == {"calls": 3}


def test_run_rlm_blocking_error(monkeypatch):
    import tools.rlm_tool as rlm_mod
    monkeypatch.setattr(
        rlm_mod, "rlm_tool",
        lambda **kw: '{"status": "error", "error": "boom"}')
    out = A._run_rlm_blocking({"rlm_args": {"query": "q"}})
    assert out["ok"] is False
    assert out["error"] == "boom"
    assert out["summary"] is None
