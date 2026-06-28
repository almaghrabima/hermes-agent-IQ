"""drain_outbox_for_sessions must skip emitting a completion event for a row
whose run_id is already present in synced SessionDB history, so a device that
reconciled the same durable run does not re-forge it."""
import logging
from unittest.mock import MagicMock

import plugins.temporal.delivery as delivery


def _row(run_id, session_key="s1", status="completed"):
    return {"run_id": run_id, "session_key": session_key, "status": status,
            "block": {"goal": "g", "summary": "done"}}


def test_skips_row_already_in_history(monkeypatch):
    monkeypatch.setattr(delivery.outbox, "claim_undelivered", lambda keys: [_row("run-1")])
    db = MagicMock()
    db.has_platform_message_id.return_value = True

    events = delivery.drain_outbox_for_sessions(["s1"], db)

    assert events == []
    db.has_platform_message_id.assert_called_once_with("s1", "run-1")


def test_emits_row_not_in_history(monkeypatch):
    monkeypatch.setattr(delivery.outbox, "claim_undelivered", lambda keys: [_row("run-2")])
    db = MagicMock()
    db.has_platform_message_id.return_value = False

    events = delivery.drain_outbox_for_sessions(["s1"], db)

    assert len(events) == 1
    assert events[0]["delegation_id"] == "run-2"


def test_emits_when_history_check_raises(monkeypatch, caplog):
    monkeypatch.setattr(delivery.outbox, "claim_undelivered", lambda keys: [_row("run-3")])
    db = MagicMock()
    db.has_platform_message_id.side_effect = RuntimeError("db down")

    with caplog.at_level(logging.WARNING, logger="plugins.temporal.delivery"):
        events = delivery.drain_outbox_for_sessions(["s1"], db)

    assert len(events) == 1  # best-effort: favor delivery over silent drop
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_no_session_db_emits_all_unchanged(monkeypatch):
    monkeypatch.setattr(delivery.outbox, "claim_undelivered", lambda keys: [_row("run-4")])
    events = delivery.drain_outbox_for_sessions(["s1"])
    assert len(events) == 1
