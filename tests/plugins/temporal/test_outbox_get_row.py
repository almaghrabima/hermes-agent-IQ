from plugins.temporal import outbox


def test_get_row_returns_record(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion("run-9:waiting", "sessA", "waiting", {"prompt": "ok?"})
    row = outbox.get_row("run-9:waiting")
    assert row["session_key"] == "sessA"
    assert row["status"] == "waiting"
    assert row["block"]["prompt"] == "ok?"


def test_get_row_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert outbox.get_row("nope") is None
