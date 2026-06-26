import json
from plugins.temporal import outbox

def test_record_and_claim_marks_delivered(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("run-1", "sessA", "completed", {"goal": "g", "summary": "s"})
    rows = outbox.claim_undelivered(["sessA"])
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["session_key"] == "sessA"
    assert rows[0]["block"]["summary"] == "s"
    # second claim returns nothing (already delivered) -> no double delivery
    assert outbox.claim_undelivered(["sessA"]) == []

def test_record_is_idempotent_on_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("run-2", "s", "completed", {"summary": "a"})
    outbox.record_completion("run-2", "s", "completed", {"summary": "b"})  # ignored
    rows = outbox.claim_undelivered(["s"])
    assert len(rows) == 1
    assert rows[0]["block"]["summary"] == "a"

def test_claim_filters_by_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("r1", "A", "completed", {})
    outbox.record_completion("r2", "B", "completed", {})
    assert [r["run_id"] for r in outbox.claim_undelivered(["A"])] == ["r1"]
    assert [r["run_id"] for r in outbox.claim_undelivered(["B"])] == ["r2"]

def test_has_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert outbox.has_run("x") is False
    outbox.record_completion("x", "s", "completed", {})
    assert outbox.has_run("x") is True
