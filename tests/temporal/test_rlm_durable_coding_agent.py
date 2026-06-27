import json

import plugins.temporal.activities as activities
import tools.rlm_tool as rlm_tool


def test_durable_blocking_path_uses_coding_agent_backend(monkeypatch):
    # _run_rlm_blocking calls the real sync rlm_tool; stub the tool to assert the
    # payload threads through and returns a normalized durable result.

    def fake_rlm_tool(**kw):
        assert kw["query"] == "Q"
        return json.dumps({"status": "success", "model_backend": "coding_agent:openai-codex",
                           "result": "DONE", "usage": {"calls": 1}, "log_path": "/x.log"})

    monkeypatch.setattr(rlm_tool, "rlm_tool", fake_rlm_tool)
    out = activities._run_rlm_blocking({"rlm_args": {"query": "Q"}, "run_id": "r1"})
    assert out["ok"] is True
    assert out["summary"] == "DONE"
