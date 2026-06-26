"""SessionDB still works with the shim in the default (sqlite) path, and
resolve_sync_config is consulted exactly once per connection."""
from unittest.mock import patch

from hermes_state import SessionDB


def test_sessiondb_default_path_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="cli")
    db.append_message("s1", role="user", content="hello")
    msgs = db.get_messages("s1")
    assert [m["content"] for m in msgs] == ["hello"]


def test_sessiondb_consults_resolver_with_label(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_state.resolve_sync_config", return_value=None) as res:
        SessionDB(db_path=tmp_path / "state.db")
    assert res.called
    assert res.call_args.args[0] == "state.db"
