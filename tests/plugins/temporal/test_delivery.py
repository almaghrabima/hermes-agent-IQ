from plugins.temporal import outbox, delivery


def test_drain_produces_async_delegation_events(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    block = {"goal": "g", "context": None, "toolsets": None, "role": "leaf",
             "model": "m", "summary": "done", "error": None}
    outbox.record_completion("run-1", "sessA", "completed", block)
    events = delivery.drain_outbox_for_sessions(["sessA"])
    assert len(events) == 1
    e = events[0]
    assert e["type"] == "async_delegation"
    assert e["session_key"] == "sessA"
    assert e["status"] == "completed"
    assert e["goal"] == "g"
    assert e["summary"] == "done"
    # drained rows are delivered -> no repeat
    assert delivery.drain_outbox_for_sessions(["sessA"]) == []
