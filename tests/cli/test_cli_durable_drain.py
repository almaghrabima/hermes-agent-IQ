"""Regression: the CLI must drain durable-delegation completions for its OWN
session id, not a hardcoded 'default'.

Bug: a durable delegation is dispatched under the live session key
(``get_current_session_key()`` -> ``HermesCLI.session_id``, a generated
``<timestamp>_<uuid>``), but the CLI's outbox drain hardcoded
``drain_outbox_for_sessions(["default"])``. Since ``session_id`` is never
literally "default" in a normal CLI run, durable completions were never
re-delivered into the conversation.
"""

import cli
from plugins.temporal import outbox


def test_cli_drains_durable_outbox_for_its_own_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sid = "20260626_120000_deadbeef"  # CLI-style generated id, not "default"

    # A completion belonging to THIS CLI session.
    outbox.record_completion(
        "run-mine", sid, "completed", {"goal": "g", "summary": "mine"}
    )
    # A completion for some OTHER session must not leak into this one.
    outbox.record_completion(
        "run-other", "default", "completed", {"goal": "g2", "summary": "theirs"}
    )

    class _FakeCLI:
        session_id = sid

    events = cli.HermesCLI._drain_durable_outbox(_FakeCLI())

    ids = [e["delegation_id"] for e in events]
    assert ids == ["run-mine"], f"expected only this session's row, got {ids}"
    assert events[0]["summary"] == "mine"


def test_cli_drain_falls_back_to_default_when_session_id_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    outbox.record_completion(
        "run-d", "default", "completed", {"goal": "g", "summary": "d"}
    )

    class _FakeCLI:
        session_id = None

    events = cli.HermesCLI._drain_durable_outbox(_FakeCLI())
    assert [e["delegation_id"] for e in events] == ["run-d"]
