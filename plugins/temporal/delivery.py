from __future__ import annotations
import logging
from plugins.temporal import outbox

logger = logging.getLogger(__name__)


def _row_to_event(row: dict) -> dict:
    """Convert an outbox row into a type='async_delegation' completion event,
    matching tools/async_delegation.py's _push_completion_event shape so the
    existing CLI/gateway drains forge a turn identically."""
    b = row.get("block") or {}
    return {
        "type": "async_delegation",
        "delegation_id": row["run_id"],
        "session_key": row["session_key"],
        "goal": b.get("goal", ""),
        "context": b.get("context"),
        "toolsets": b.get("toolsets"),
        "role": b.get("role"),
        "model": b.get("model"),
        "status": row.get("status", b.get("status", "completed")),
        "summary": b.get("summary"),
        "error": b.get("error"),
        "api_calls": b.get("api_calls", 0),
        "duration_seconds": b.get("duration_seconds"),
        "dispatched_at": b.get("dispatched_at"),
        "completed_at": b.get("completed_at"),
        "exit_reason": b.get("exit_reason"),
        "durable": True,
    }


def _already_surfaced(session_db, row: dict) -> bool:
    """True if a message tagged with this run_id already exists in (synced)
    history — i.e. another device already delivered it. Best-effort: on any
    SessionDB error, return False so we favor delivery over a silent drop."""
    if session_db is None:
        return False
    try:
        return bool(
            session_db.has_platform_message_id(row["session_key"], row["run_id"])
        )
    except Exception as exc:  # noqa: BLE001 — never block delivery on a read error
        logger.warning("durable dedup check failed for %s: %s", row.get("run_id"), exc)
        return False


def drain_outbox_for_sessions(session_keys: list[str], session_db=None) -> list[dict]:
    """Claim undelivered durable results for these sessions and return them as
    completion events (already marked delivered).

    When ``session_db`` is provided, rows whose ``run_id`` is already present in
    SessionDB history are skipped: their result was delivered (and synced) by
    another device, so re-forging here would double-surface it. When
    ``session_db`` is None, every claimed row is emitted (legacy behavior)."""
    rows = outbox.claim_undelivered(session_keys)
    return [_row_to_event(r) for r in rows if not _already_surfaced(session_db, r)]


def reconcile_from_temporal() -> int:
    """Best-effort startup backfill: record completed durable delegations that are
    missing from the outbox (e.g. finished while no Hermes process was alive).
    Returns the number of rows inserted. No-op if temporal is unavailable."""
    try:
        from plugins.temporal.tools import list_completed_durable_delegations  # Task 4
    except Exception:
        return 0
    inserted = 0
    try:
        for item in list_completed_durable_delegations():
            if not outbox.has_run(item["run_id"]):
                outbox.record_completion(item["run_id"], item["session_key"], item["status"], item["block"])
                inserted += 1
    except Exception as exc:  # best-effort
        logger.warning("temporal reconcile skipped: %s", exc)
    try:
        from plugins.temporal.tools import list_completed_durable_rlm
        for item in list_completed_durable_rlm():
            if not outbox.has_run(item["run_id"]):
                outbox.record_completion(item["run_id"], item["session_key"], item["status"], item["block"])
                inserted += 1
    except Exception as exc:  # best-effort
        logger.warning("temporal rlm reconcile skipped: %s", exc)
    return inserted
